"""Accounting models for the EA Accounting Application.

Scope: chart of accounts, partners, journals, double-entry journal entries
(with debits=credits validation), AR/AP sub-ledger, and fixed-asset
depreciation. Modeled loosely on Odoo's account.* hierarchy but trimmed.

Multi-tenancy (Phase 0.1): every business model gets a `tenant` FK to the
new `Tenant` model. The FK is nullable in this migration; a follow-up will
tighten it to NOT NULL once the middleware and managers are in place.
"""

from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class Tenant(models.Model):
    """A customer organisation on the SaaS. One Tenant == one set of books.

    A user can belong to many tenants via `Membership` (e.g. an accountant
    serving multiple SMEs). The tenant's slug becomes its subdomain
    (e.g. `elite.ealedgers.com`) once we wire that up.
    """

    BUSINESS_TYPES = [
        ("services", "Services"),
        ("goods", "Goods"),
        ("both", "Services & Goods"),
    ]
    PLANS = [
        ("free", "Free"),
        ("starter", "Starter"),
        ("pro", "Pro"),
        ("enterprise", "Enterprise"),
    ]

    slug = models.SlugField(max_length=64, unique=True, help_text="URL-safe identifier; becomes subdomain")
    name = models.CharField(max_length=128)
    legal_name = models.CharField(max_length=256, blank=True)
    country = models.CharField(max_length=64, blank=True, help_text="e.g. Cameroon")
    currency = models.ForeignKey(
        "Currency", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text="Default reporting currency (e.g. XAF for OHADA)",
    )
    business_type = models.CharField(max_length=16, choices=BUSINESS_TYPES, default="both")
    fiscal_year_start_month = models.IntegerField(default=1, help_text="1=January")
    tax_id = models.CharField(max_length=32, blank=True, verbose_name="Tax ID / NIU")
    company_registry = models.CharField(max_length=64, blank=True, verbose_name="RCCM")
    plan = models.CharField(max_length=16, choices=PLANS, default="free")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="owned_tenants",
        help_text="The user who created the tenant and pays the bill",
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Membership(models.Model):
    """Joins a user to a tenant with a role. A user can have memberships in
    multiple tenants (e.g. an accountant serving several SMEs)."""

    ROLES = [
        ("owner", "Owner"),
        ("admin", "Admin"),
        ("accountant", "Accountant"),
        ("viewer", "Viewer"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=ROLES, default="accountant")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "tenant")]
        ordering = ["tenant", "user"]

    def __str__(self):
        return f"{self.user} @ {self.tenant} ({self.get_role_display()})"


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


class Currency(models.Model):
    name = models.CharField(max_length=64, unique=True)
    code = models.CharField(max_length=8, unique=True, help_text='ISO code, e.g. XAF, EUR, USD')
    symbol = models.CharField(max_length=8, blank=True)
    decimal_places = models.IntegerField(default=2, help_text='0 for XAF, 2 for EUR/USD')
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['code']
        verbose_name_plural = 'currencies'

    def __str__(self):
        return self.code


class Company(models.Model):
    name = models.CharField(max_length=128)
    legal_name = models.CharField(max_length=256, blank=True)
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name='+')
    tax_id = models.CharField(max_length=32, blank=True, verbose_name='Tax ID / NIU')
    company_registry = models.CharField(max_length=64, blank=True, verbose_name='RCCM')
    street = models.CharField(max_length=256, blank=True)
    city = models.CharField(max_length=128, blank=True)
    country = models.CharField(max_length=64, blank=True)
    fiscal_year_start_month = models.IntegerField(default=1, help_text='1=January')
    active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = 'companies'

    def __str__(self):
        return self.name


class Partner(models.Model):
    """A customer, vendor, or both. Drives the AR/AP sub-ledgers."""

    PARTNER_TYPES = [
        ('customer', 'Customer'),
        ('vendor', 'Vendor'),
        ('both', 'Customer & Vendor'),
        ('other', 'Other'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name='partners',
    )
    name = models.CharField(max_length=256)
    partner_type = models.CharField(max_length=16, choices=PARTNER_TYPES, default='customer')
    is_company = models.BooleanField(default=True)
    tax_id = models.CharField(max_length=32, blank=True, verbose_name='Tax ID / NIU')
    company_registry = models.CharField(max_length=64, blank=True, verbose_name='RCCM')

    street = models.CharField(max_length=256, blank=True)
    street2 = models.CharField(max_length=256, blank=True)
    city = models.CharField(max_length=128, blank=True)
    zip = models.CharField(max_length=32, blank=True)
    country = models.CharField(max_length=64, blank=True)

    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    website = models.CharField(max_length=256, blank=True)

    account_receivable = models.ForeignKey(
        'Account', on_delete=models.PROTECT, null=True, blank=True,
        related_name='partners_as_receivable',
        limit_choices_to={'type': 'receivable'},
        help_text='Default AR account for this customer (usually 411x)',
    )
    account_payable = models.ForeignKey(
        'Account', on_delete=models.PROTECT, null=True, blank=True,
        related_name='partners_as_payable',
        limit_choices_to={'type': 'payable'},
        help_text='Default AP account for this vendor (usually 401x)',
    )
    credit_limit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Chart of accounts and journals
# ---------------------------------------------------------------------------


class Account(models.Model):
    """A single account in the chart. Maps to Odoo's account.account / SYSCOHADA account."""

    TYPES = [
        # Assets
        ('receivable', 'Receivable'),
        ('asset_cash', 'Bank and Cash'),
        ('asset_current', 'Current Assets'),
        ('asset_non_current', 'Non-current Assets'),
        ('asset_prepayments', 'Prepayments'),
        ('asset_fixed', 'Fixed Assets'),
        # Liabilities
        ('payable', 'Payable'),
        ('liability_credit_card', 'Credit Card'),
        ('liability_current', 'Current Liabilities'),
        ('liability_non_current', 'Non-current Liabilities'),
        # Equity
        ('equity', 'Equity'),
        ('equity_unaffected', 'Current Year Earnings'),
        # Income
        ('income', 'Income'),
        ('income_other', 'Other Income'),
        # Expenses
        ('expense', 'Expenses'),
        ('expense_depreciation', 'Depreciation'),
        ('expense_direct_cost', 'Cost of Revenue'),
        ('expense_other', 'Other Expenses'),
        # Off-balance
        ('off_balance_sheet', 'Off-Balance Sheet'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name='accounts',
    )
    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=256)
    type = models.CharField(max_length=32, choices=TYPES)
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reconcile = models.BooleanField(
        default=False, help_text='Allow reconciling lines on this account (typical for AR/AP)'
    )
    deprecated = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f'{self.code} — {self.name}'

    @property
    def syscohada_class(self):
        """First digit of the account code = SYSCOHADA class (1..9)."""
        return self.code[0] if self.code else ''


class Journal(models.Model):
    TYPES = [
        ('sale', 'Sales'),
        ('purchase', 'Purchases'),
        ('cash', 'Cash'),
        ('bank', 'Bank'),
        ('general', 'General / Miscellaneous'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name='journals',
    )
    name = models.CharField(max_length=128)
    code = models.CharField(max_length=8, unique=True, help_text='Short code, e.g. VEN, ACH, BNK, OD')
    type = models.CharField(max_length=16, choices=TYPES)
    default_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True, related_name='+',
        help_text='Default account for entries in this journal (e.g. the bank account for a BNK journal)',
    )
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    sequence_prefix = models.CharField(max_length=16, default='', help_text='e.g. "VEN/", "ACH/"')
    next_sequence = models.IntegerField(default=1)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f'{self.code} — {self.name}'

    def next_entry_name(self):
        """Atomically reserve and return the next entry name for this journal."""
        with transaction.atomic():
            j = Journal.objects.select_for_update().get(pk=self.pk)
            n = j.next_sequence
            j.next_sequence = n + 1
            j.save(update_fields=['next_sequence'])
            return f'{j.sequence_prefix}{n:05d}'


# ---------------------------------------------------------------------------
# Double-entry journal entries
# ---------------------------------------------------------------------------


class JournalEntry(models.Model):
    """A balanced set of debits/credits. Maps to Odoo's account.move."""

    STATES = [
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancelled', 'Cancelled'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name='journal_entries',
    )
    name = models.CharField(max_length=64, blank=True, help_text='Auto-assigned on posting from the journal sequence')
    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name='entries')
    date = models.DateField()
    ref = models.CharField(max_length=128, blank=True, verbose_name='Reference')
    partner = models.ForeignKey(
        Partner, on_delete=models.PROTECT, null=True, blank=True, related_name='journal_entries'
    )
    state = models.CharField(max_length=16, choices=STATES, default='draft')
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-date', '-id']
        verbose_name_plural = 'journal entries'

    def __str__(self):
        return self.name or f'Draft #{self.id}'

    # --- aggregates ---------------------------------------------------------

    @property
    def total_debit(self):
        return self.lines.aggregate(s=models.Sum('debit'))['s'] or Decimal('0')

    @property
    def total_credit(self):
        return self.lines.aggregate(s=models.Sum('credit'))['s'] or Decimal('0')

    @property
    def is_balanced(self):
        return self.total_debit == self.total_credit

    # --- state transitions --------------------------------------------------

    def post(self):
        """Validate the double-entry constraint and post the entry. Idempotent for posted."""
        if self.state == 'posted':
            return
        if self.state == 'cancelled':
            raise ValidationError('Cannot post a cancelled entry.')
        if self.lines.count() < 2:
            raise ValidationError('A journal entry must have at least two lines.')
        if self.total_debit == 0:
            raise ValidationError('Entry has zero total — nothing to post.')
        if not self.is_balanced:
            raise ValidationError(
                f'Debits ({self.total_debit}) do not equal credits ({self.total_credit}).'
            )
        if not self.name:
            self.name = self.journal.next_entry_name()
        self.state = 'posted'
        self.posted_at = timezone.now()
        self.save()

    def cancel(self):
        if self.state == 'posted':
            raise ValidationError(
                'Cannot cancel a posted entry. Create a reversing entry instead.'
            )
        self.state = 'cancelled'
        self.save()


class JournalEntryLine(models.Model):
    """A single debit or credit line. Maps to Odoo's account.move.line."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name='journal_entry_lines',
    )
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='lines')
    partner = models.ForeignKey(
        Partner, on_delete=models.PROTECT, null=True, blank=True, related_name='ledger_lines',
        help_text='Required for receivable/payable lines (drives the AR/AP sub-ledger)',
    )
    name = models.CharField(max_length=256, blank=True, help_text='Description / memo')
    debit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    credit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    reconciled = models.BooleanField(default=False)
    reconciled_with = models.ManyToManyField('self', blank=True, symmetrical=True)

    class Meta:
        ordering = ['entry', 'id']

    def __str__(self):
        return f'{self.account.code} D{self.debit}/C{self.credit}'

    def clean(self):
        if self.debit < 0 or self.credit < 0:
            raise ValidationError('Debit and credit must be non-negative.')
        if self.debit > 0 and self.credit > 0:
            raise ValidationError('A line cannot have both debit and credit set.')
        if self.debit == 0 and self.credit == 0:
            raise ValidationError('A line must have either a debit or a credit greater than zero.')
        if self.account_id and self.account.type in ('receivable', 'payable') and not self.partner_id:
            raise ValidationError(
                f'Account {self.account.code} ({self.account.get_type_display()}) requires a partner.'
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Fixed assets & depreciation
# ---------------------------------------------------------------------------


class FixedAsset(models.Model):
    """A depreciable asset. Generates a per-period schedule and can auto-post entries."""

    METHODS = [
        ('straight_line', 'Straight-line'),
        ('declining', 'Declining balance'),
    ]
    STATES = [
        ('draft', 'Draft'),
        ('in_use', 'In use'),
        ('fully_depreciated', 'Fully depreciated'),
        ('disposed', 'Disposed'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name='fixed_assets',
    )
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=256)
    purchase_date = models.DateField()
    in_service_date = models.DateField(help_text='Depreciation starts on this date')
    purchase_cost = models.DecimalField(max_digits=18, decimal_places=2)
    salvage_value = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    useful_life_months = models.IntegerField(help_text='e.g. 60 for 5 years')
    method = models.CharField(max_length=16, choices=METHODS, default='straight_line')
    declining_rate = models.DecimalField(
        max_digits=5, decimal_places=4, default=Decimal('0'),
        help_text='Annual rate, e.g. 0.20 for 20%. Used only for declining-balance method.',
    )

    asset_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='+',
        help_text='Class 2 — the asset (e.g. 244x for office equipment)',
    )
    accumulated_depreciation_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='+',
        help_text='Contra-asset (typically class 28xx)',
    )
    depreciation_expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='+',
        help_text='Class 68xx — depreciation expense',
    )
    depreciation_journal = models.ForeignKey(
        Journal, on_delete=models.PROTECT, related_name='depreciation_assets',
        help_text='Journal where periodic depreciation entries will post (typically OD/General)',
    )

    state = models.CharField(max_length=24, choices=STATES, default='draft')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f'{self.code} — {self.name}'

    @property
    def depreciable_basis(self):
        return self.purchase_cost - self.salvage_value

    @property
    def total_posted(self):
        return self.depreciation_lines.filter(posted=True).aggregate(
            s=models.Sum('amount')
        )['s'] or Decimal('0')

    @property
    def book_value(self):
        return self.purchase_cost - self.total_posted

    # --- schedule generation -----------------------------------------------

    def generate_schedule(self):
        """(Re)compute the depreciation schedule. Wipes any unposted lines first."""
        self.depreciation_lines.filter(posted=False).delete()
        if self.method == 'straight_line':
            self._generate_straight_line()
        else:
            self._generate_declining()

    def _generate_straight_line(self):
        monthly = (self.depreciable_basis / self.useful_life_months).quantize(Decimal('0.01'))
        current = self.in_service_date
        for _ in range(self.useful_life_months):
            period_end = current + relativedelta(months=1) - relativedelta(days=1)
            self.depreciation_lines.create(period_date=period_end, amount=monthly)
            current += relativedelta(months=1)

    def _generate_declining(self):
        book_value = self.purchase_cost
        current = self.in_service_date
        months_left = self.useful_life_months
        while months_left > 0 and book_value > self.salvage_value:
            annual = (book_value - self.salvage_value) * self.declining_rate
            monthly = (annual / 12).quantize(Decimal('0.01'))
            months_this_year = min(12, months_left)
            for _ in range(months_this_year):
                period_end = current + relativedelta(months=1) - relativedelta(days=1)
                self.depreciation_lines.create(period_date=period_end, amount=monthly)
                current += relativedelta(months=1)
                book_value -= monthly
                months_left -= 1
                if book_value <= self.salvage_value:
                    break

    # --- posting -----------------------------------------------------------

    def post_depreciation(self, up_to_date):
        """Post all unposted lines with period_date <= up_to_date. Returns the count posted."""
        lines = self.depreciation_lines.filter(
            posted=False, period_date__lte=up_to_date
        ).order_by('period_date')
        posted = 0
        for line in lines:
            entry = JournalEntry.objects.create(
                journal=self.depreciation_journal,
                date=line.period_date,
                ref=f'Depreciation {self.code}',
                notes=f'Auto-generated depreciation — {self.code} {self.name}',
            )
            JournalEntryLine.objects.create(
                entry=entry,
                account=self.depreciation_expense_account,
                name=f'Dep. {self.code}',
                debit=line.amount,
            )
            JournalEntryLine.objects.create(
                entry=entry,
                account=self.accumulated_depreciation_account,
                name=f'Dep. {self.code}',
                credit=line.amount,
            )
            entry.post()
            line.posted = True
            line.journal_entry = entry
            line.save()
            posted += 1
        # transition state if we just fully depreciated
        if self.book_value <= self.salvage_value and self.state == 'in_use':
            self.state = 'fully_depreciated'
            self.save(update_fields=['state'])
        return posted


class DepreciationLine(models.Model):
    """A single period's planned depreciation. Becomes posted=True once its journal entry exists."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name='depreciation_lines_for_tenant',
    )
    asset = models.ForeignKey(FixedAsset, on_delete=models.CASCADE, related_name='depreciation_lines')
    period_date = models.DateField(help_text='End of the period being depreciated')
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    posted = models.BooleanField(default=False)
    journal_entry = models.ForeignKey(
        JournalEntry, on_delete=models.PROTECT, null=True, blank=True, related_name='depreciation_lines'
    )

    class Meta:
        ordering = ['asset', 'period_date']

    def __str__(self):
        return f'{self.asset.code} {self.period_date}: {self.amount}'
