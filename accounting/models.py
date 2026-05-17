"""Accounting models for the EA Accounting Application.

Scope: chart of accounts, partners, journals, double-entry journal entries
(with debits=credits validation), AR/AP sub-ledger, and fixed-asset
depreciation. Modeled loosely on Odoo's account.* hierarchy but trimmed.

Multi-tenancy (Phase 0.1): every business model gets a `tenant` FK to the
new `Tenant` model. The FK is nullable in this migration; a follow-up will
tighten it to NOT NULL once the middleware and managers are in place.
"""

from datetime import timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from accounting.managers import TenantManager


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
    """A customer, vendor, or both. Drives the AR/AP sub-ledgers.

    Tenant-scoped: each tenant has its own list of partners.
    """

    PARTNER_TYPES = [
        ('customer', 'Customer'),
        ('vendor', 'Vendor'),
        ('both', 'Customer & Vendor'),
        ('other', 'Other'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='partners',
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

    objects = TenantManager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Chart of accounts and journals
# ---------------------------------------------------------------------------


class Account(models.Model):
    """A single account in the chart. Maps to Odoo's account.account / SYSCOHADA account.

    ``code`` is unique **per tenant** (enforced by ``unique_account_code_per_tenant``).
    """

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
        Tenant, on_delete=models.CASCADE, related_name='accounts',
    )
    code = models.CharField(max_length=16)
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
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'code'], name='unique_account_code_per_tenant'),
        ]

    def __str__(self):
        return f'{self.code} — {self.name}'

    @property
    def syscohada_class(self):
        """First digit of the account code = SYSCOHADA class (1..9)."""
        return self.code[0] if self.code else ''

    # Tenant-aware manager (Account.objects.for_tenant(tenant))
    objects = TenantManager()


class Journal(models.Model):
    TYPES = [
        ('sale', 'Sales'),
        ('purchase', 'Purchases'),
        ('cash', 'Cash'),
        ('bank', 'Bank'),
        ('general', 'General / Miscellaneous'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='journals',
    )
    name = models.CharField(max_length=128)
    code = models.CharField(max_length=8, help_text='Short code, e.g. VEN, ACH, BNK, OD')
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
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'code'], name='unique_journal_code_per_tenant'),
        ]

    objects = TenantManager()

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
    """A balanced set of debits/credits. Maps to Odoo's account.move.

    Tenant-scoped.
    """

    STATES = [
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancelled', 'Cancelled'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='journal_entries',
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

    objects = TenantManager()

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
    """A single debit or credit line. Maps to Odoo's account.move.line.

    Tenant-scoped. The tenant must match the parent entry's tenant; that
    invariant is enforced in ``clean()``.
    """

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='journal_entry_lines',
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

    objects = TenantManager()

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
        # Tenant must match the parent entry's tenant (defence in depth)
        if self.entry_id and self.tenant_id and self.entry.tenant_id != self.tenant_id:
            raise ValidationError("Line tenant must match the parent entry's tenant.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Fixed assets & depreciation
# ---------------------------------------------------------------------------


class FixedAsset(models.Model):
    """A depreciable asset. Generates a per-period schedule and can auto-post entries.

    ``code`` is unique per tenant.
    """

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
        Tenant, on_delete=models.CASCADE, related_name='fixed_assets',
    )
    code = models.CharField(max_length=32)
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

    objects = TenantManager()

    class Meta:
        ordering = ['code']
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'code'], name='unique_fixedasset_code_per_tenant'),
        ]

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
    """A single period's planned depreciation. Becomes posted=True once its journal entry exists.

    Tenant-scoped.
    """

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='depreciation_lines_for_tenant',
    )
    asset = models.ForeignKey(FixedAsset, on_delete=models.CASCADE, related_name='depreciation_lines')
    period_date = models.DateField(help_text='End of the period being depreciated')
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    posted = models.BooleanField(default=False)
    journal_entry = models.ForeignKey(
        JournalEntry, on_delete=models.PROTECT, null=True, blank=True, related_name='depreciation_lines'
    )

    objects = TenantManager()

    class Meta:
        ordering = ['asset', 'period_date']

    def __str__(self):
        return f'{self.asset.code} {self.period_date}: {self.amount}'


# ---------------------------------------------------------------------------
# Customer invoicing (Phase 1.1)
# ---------------------------------------------------------------------------

# SYSCOHADA account codes the invoicing engine looks up by convention. Each
# tenant ships with the full chart preloaded, so these are reliable defaults.
SYSCOHADA_VAT_COLLECTED = '4434'       # TVA facturée (sales)
SYSCOHADA_VAT_RECOVERABLE = '4451'     # TVA récupérable (purchases)
SYSCOHADA_WHT_CREDIT = '4423'          # Acomptes versés (WHT credit when WE sell)
SYSCOHADA_WHT_PAYABLE = '4424'         # Acomptes reçus (WHT payable when WE buy)


class CustomerInvoice(models.Model):
    """A customer-facing invoice in draft → posted → paid lifecycle.

    Posting creates a balanced JournalEntry:
      Dr 411x Customer:          total - withholding
      Dr 4423 WHT credit:        withholding_amount      (if WHT > 0)
      Cr 70x  Revenue (per line): line.amount_untaxed
      Cr 4434 VAT collected:     vat_amount              (if any line has tax)

    Recording payment creates a second JournalEntry on the chosen
    bank/cash journal:
      Dr Bank/Cash:              total - withholding
      Cr 411x Customer:          total - withholding
    """

    STATES = [
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('paid', 'Paid'),
        ('cancelled', 'Cancelled'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='customer_invoices',
    )
    number = models.CharField(
        max_length=32, blank=True,
        help_text='Auto-assigned on posting from the sales journal sequence',
    )
    partner = models.ForeignKey(
        Partner, on_delete=models.PROTECT, related_name='customer_invoices',
        limit_choices_to={'partner_type__in': ['customer', 'both']},
    )
    journal = models.ForeignKey(
        Journal, on_delete=models.PROTECT, related_name='customer_invoices',
        limit_choices_to={'type': 'sale'},
        help_text='Sales journal used for posting',
    )
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name='+',
    )
    date = models.DateField(help_text='Invoice date')
    due_date = models.DateField(help_text='Payment due')
    state = models.CharField(max_length=16, choices=STATES, default='draft')

    withholding_tax_rate = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal('0'),
        help_text='Percentage withheld at source by the buyer (e.g. 10.00 for 10%)',
    )

    # Denormalised totals (recomputed from lines on save). Stored so
    # reports stay fast without an aggregate join.
    amount_subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    amount_tax = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    amount_withholding = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    amount_total = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    notes = models.TextField(blank=True)

    # Audit / state pointers
    journal_entry = models.ForeignKey(
        JournalEntry, on_delete=models.PROTECT, null=True, blank=True,
        related_name='customer_invoice_post',
        help_text='Set once the invoice is posted to the GL',
    )
    payment_entry = models.ForeignKey(
        JournalEntry, on_delete=models.PROTECT, null=True, blank=True,
        related_name='customer_invoice_payment',
        help_text='Set once payment has been recorded',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'number'],
                name='unique_invoice_number_per_tenant',
                condition=~models.Q(number=''),
            ),
        ]

    def __str__(self):
        return self.number or f'Draft invoice #{self.id}'

    # --- amounts -----------------------------------------------------------

    def recompute_amounts(self, save=True):
        """Recompute denormalised totals from current lines."""
        subtotal = Decimal('0')
        tax = Decimal('0')
        for line in self.lines.all():
            subtotal += line.amount_untaxed
            tax += line.amount_tax
        total = subtotal + tax
        wht = (total * self.withholding_tax_rate / 100).quantize(Decimal('0.01'))
        self.amount_subtotal = subtotal
        self.amount_tax = tax
        self.amount_total = total
        self.amount_withholding = wht
        if save:
            self.save(update_fields=[
                'amount_subtotal', 'amount_tax', 'amount_total',
                'amount_withholding', 'updated_at',
            ])

    @property
    def amount_net_receivable(self):
        """What the customer actually owes (total minus the WHT they keep)."""
        return self.amount_total - self.amount_withholding

    # --- account lookups (defaults from chart of accounts) ----------------

    def _receivable_account(self):
        if self.partner.account_receivable_id:
            return self.partner.account_receivable
        # First receivable-type account for this tenant
        acc = Account.objects.for_tenant(self.tenant).filter(
            type='receivable', deprecated=False,
        ).order_by('code').first()
        if acc is None:
            raise ValidationError(
                'No receivable account configured for the tenant. '
                'Add one with type=Receivable in the chart of accounts, '
                'or set Partner.account_receivable on the customer.',
            )
        return acc

    def _vat_account(self):
        acc = Account.objects.for_tenant(self.tenant).filter(
            code=SYSCOHADA_VAT_COLLECTED, deprecated=False,
        ).first()
        if acc is None:
            raise ValidationError(
                f'VAT account {SYSCOHADA_VAT_COLLECTED} not found in the '
                f'chart of accounts. Lines with tax > 0 cannot be posted.',
            )
        return acc

    def _wht_account(self):
        acc = Account.objects.for_tenant(self.tenant).filter(
            code=SYSCOHADA_WHT_CREDIT, deprecated=False,
        ).first()
        if acc is None:
            raise ValidationError(
                f'Withholding-tax credit account {SYSCOHADA_WHT_CREDIT} '
                f'not found in the chart of accounts.',
            )
        return acc

    # --- state transitions -------------------------------------------------

    @transaction.atomic
    def post(self):
        """Validate and post the invoice. Creates and posts the journal entry."""
        if self.state == 'posted' or self.state == 'paid':
            return
        if self.state == 'cancelled':
            raise ValidationError('Cannot post a cancelled invoice.')
        if not self.lines.exists():
            raise ValidationError('Invoice has no lines — nothing to post.')

        self.recompute_amounts(save=False)
        if self.amount_total <= 0:
            raise ValidationError('Invoice total must be positive.')

        receivable = self._receivable_account()
        vat = self._vat_account() if self.amount_tax > 0 else None
        wht = self._wht_account() if self.amount_withholding > 0 else None

        if not self.number:
            self.number = self.journal.next_entry_name()

        entry = JournalEntry.objects.create(
            tenant=self.tenant,
            journal=self.journal,
            date=self.date,
            ref=f'Customer invoice {self.number}',
            partner=self.partner,
            notes=f'Posted from CustomerInvoice {self.number}',
        )

        # Dr Receivable (net of WHT)
        JournalEntryLine.objects.create(
            tenant=self.tenant, entry=entry, account=receivable, partner=self.partner,
            name=f'INV {self.number}',
            debit=self.amount_net_receivable, credit=Decimal('0'),
        )
        # Dr WHT credit
        if wht is not None:
            JournalEntryLine.objects.create(
                tenant=self.tenant, entry=entry, account=wht, partner=self.partner,
                name=f'WHT {self.number}',
                debit=self.amount_withholding, credit=Decimal('0'),
            )
        # Cr Revenue (per line)
        for line in self.lines.all():
            JournalEntryLine.objects.create(
                tenant=self.tenant, entry=entry, account=line.account, partner=self.partner,
                name=line.description[:64] or f'INV {self.number}',
                debit=Decimal('0'), credit=line.amount_untaxed,
            )
        # Cr VAT collected
        if vat is not None:
            JournalEntryLine.objects.create(
                tenant=self.tenant, entry=entry, account=vat, partner=self.partner,
                name=f'VAT {self.number}',
                debit=Decimal('0'), credit=self.amount_tax,
            )

        entry.post()
        self.journal_entry = entry
        self.state = 'posted'
        self.posted_at = timezone.now()
        self.save()

    @transaction.atomic
    def record_payment(self, bank_journal, payment_date=None):
        """Record full payment via the given bank/cash journal."""
        if self.state != 'posted':
            raise ValidationError('Only posted invoices can be paid.')
        if not bank_journal.default_account_id:
            raise ValidationError(
                f'Journal {bank_journal.code} has no default account configured.',
            )
        if bank_journal.type not in ('bank', 'cash'):
            raise ValidationError('Payment journal must be of type bank or cash.')

        receivable = self._receivable_account()
        payment_date = payment_date or timezone.now().date()
        net = self.amount_net_receivable

        entry = JournalEntry.objects.create(
            tenant=self.tenant,
            journal=bank_journal,
            date=payment_date,
            ref=f'Payment for {self.number}',
            partner=self.partner,
            notes=f'Payment recorded against CustomerInvoice {self.number}',
        )
        JournalEntryLine.objects.create(
            tenant=self.tenant, entry=entry, account=bank_journal.default_account,
            partner=self.partner, name=f'Pay {self.number}',
            debit=net, credit=Decimal('0'),
        )
        JournalEntryLine.objects.create(
            tenant=self.tenant, entry=entry, account=receivable,
            partner=self.partner, name=f'Pay {self.number}',
            debit=Decimal('0'), credit=net,
        )
        entry.post()

        self.payment_entry = entry
        self.state = 'paid'
        self.paid_at = timezone.now()
        self.save()

    def cancel(self):
        """Only draft invoices can be cancelled. Posted ones need a credit note."""
        if self.state != 'draft':
            raise ValidationError(
                'Only draft invoices can be cancelled. '
                'Issue a credit note for posted invoices.',
            )
        self.state = 'cancelled'
        self.save(update_fields=['state', 'updated_at'])


class CustomerInvoiceLine(models.Model):
    """A single line on a customer invoice."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='customer_invoice_lines',
    )
    invoice = models.ForeignKey(
        CustomerInvoice, on_delete=models.CASCADE, related_name='lines',
    )
    sequence = models.IntegerField(default=10, help_text='Display order on the invoice')
    description = models.CharField(max_length=512)
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='+',
        limit_choices_to={'type__in': ['income', 'income_other']},
        help_text='Revenue account (e.g. 70x in SYSCOHADA)',
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('1'))
    unit_price = models.DecimalField(max_digits=18, decimal_places=2)
    tax_rate = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal('0'),
        help_text='VAT percentage on this line (e.g. 19.25 for OHADA standard rate)',
    )

    objects = TenantManager()

    class Meta:
        ordering = ['invoice', 'sequence', 'id']

    def __str__(self):
        return f'{self.description} ({self.amount})'

    @property
    def amount_untaxed(self):
        return (self.quantity * self.unit_price).quantize(Decimal('0.01'))

    @property
    def amount_tax(self):
        return (self.amount_untaxed * self.tax_rate / 100).quantize(Decimal('0.01'))

    @property
    def amount(self):
        return self.amount_untaxed + self.amount_tax


# ---------------------------------------------------------------------------
# Supplier bills (Phase 1.2)
# ---------------------------------------------------------------------------


class SupplierBill(models.Model):
    """A vendor bill in draft → posted → paid lifecycle. Mirror of
    CustomerInvoice but on the buy side.

    Posting creates a balanced JournalEntry:
      Dr 6xx Expense (per line):  line.amount_untaxed
      Dr 4451 VAT recoverable:    vat_amount               (if any line taxed)
      Cr 401x Vendor:             total - withholding      (net we owe vendor)
      Cr 4424 WHT payable to gov: withholding_amount       (if WHT > 0)

    Recording payment creates a second JournalEntry on the chosen
    bank/cash journal:
      Dr 401x Vendor:             total - withholding
      Cr Bank/Cash:               total - withholding

    The WHT liability stays on 4424 until separately remitted to the
    tax authority (Phase 2.2).
    """

    STATES = [
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('paid', 'Paid'),
        ('cancelled', 'Cancelled'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='supplier_bills',
    )
    number = models.CharField(
        max_length=32, blank=True,
        help_text='Internal reference, auto-assigned on posting from the '
                  'purchases journal sequence',
    )
    vendor_reference = models.CharField(
        max_length=64, blank=True,
        help_text="The vendor's own invoice number (printed on their PDF)",
    )
    partner = models.ForeignKey(
        Partner, on_delete=models.PROTECT, related_name='supplier_bills',
        limit_choices_to={'partner_type__in': ['vendor', 'both']},
    )
    journal = models.ForeignKey(
        Journal, on_delete=models.PROTECT, related_name='supplier_bills',
        limit_choices_to={'type': 'purchase'},
        help_text='Purchases journal used for posting',
    )
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name='+',
    )
    date = models.DateField(help_text='Bill date')
    due_date = models.DateField(help_text='Payment due')
    state = models.CharField(max_length=16, choices=STATES, default='draft')

    withholding_tax_rate = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal('0'),
        help_text='Percentage WE withhold from this vendor at source to '
                  'remit to the tax authority (e.g. 5.50 for 5.5%)',
    )

    # Denormalised totals
    amount_subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    amount_tax = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    amount_withholding = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    amount_total = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    notes = models.TextField(blank=True)

    journal_entry = models.ForeignKey(
        JournalEntry, on_delete=models.PROTECT, null=True, blank=True,
        related_name='supplier_bill_post',
    )
    payment_entry = models.ForeignKey(
        JournalEntry, on_delete=models.PROTECT, null=True, blank=True,
        related_name='supplier_bill_payment',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'number'],
                name='unique_supplier_bill_number_per_tenant',
                condition=~models.Q(number=''),
            ),
        ]

    def __str__(self):
        return self.number or f'Draft bill #{self.id}'

    # --- amounts -----------------------------------------------------------

    def recompute_amounts(self, save=True):
        subtotal = Decimal('0')
        tax = Decimal('0')
        for line in self.lines.all():
            subtotal += line.amount_untaxed
            tax += line.amount_tax
        total = subtotal + tax
        wht = (total * self.withholding_tax_rate / 100).quantize(Decimal('0.01'))
        self.amount_subtotal = subtotal
        self.amount_tax = tax
        self.amount_total = total
        self.amount_withholding = wht
        if save:
            self.save(update_fields=[
                'amount_subtotal', 'amount_tax', 'amount_total',
                'amount_withholding', 'updated_at',
            ])

    @property
    def amount_net_payable(self):
        """What we actually owe the vendor (total minus the WHT we keep)."""
        return self.amount_total - self.amount_withholding

    # --- account lookups --------------------------------------------------

    def _payable_account(self):
        if self.partner.account_payable_id:
            return self.partner.account_payable
        acc = Account.objects.for_tenant(self.tenant).filter(
            type='payable', deprecated=False,
        ).order_by('code').first()
        if acc is None:
            raise ValidationError(
                'No payable account configured for the tenant. '
                'Add one with type=Payable in the chart of accounts, '
                'or set Partner.account_payable on the vendor.',
            )
        return acc

    def _vat_account(self):
        acc = Account.objects.for_tenant(self.tenant).filter(
            code=SYSCOHADA_VAT_RECOVERABLE, deprecated=False,
        ).first()
        if acc is None:
            raise ValidationError(
                f'VAT recoverable account {SYSCOHADA_VAT_RECOVERABLE} not '
                f'found in the chart of accounts. Lines with tax > 0 '
                f'cannot be posted.',
            )
        return acc

    def _wht_account(self):
        acc = Account.objects.for_tenant(self.tenant).filter(
            code=SYSCOHADA_WHT_PAYABLE, deprecated=False,
        ).first()
        if acc is None:
            raise ValidationError(
                f'WHT-payable account {SYSCOHADA_WHT_PAYABLE} not found '
                f'in the chart of accounts.',
            )
        return acc

    # --- state transitions ------------------------------------------------

    @transaction.atomic
    def post(self):
        if self.state == 'posted' or self.state == 'paid':
            return
        if self.state == 'cancelled':
            raise ValidationError('Cannot post a cancelled bill.')
        if not self.lines.exists():
            raise ValidationError('Bill has no lines — nothing to post.')

        self.recompute_amounts(save=False)
        if self.amount_total <= 0:
            raise ValidationError('Bill total must be positive.')

        payable = self._payable_account()
        vat = self._vat_account() if self.amount_tax > 0 else None
        wht = self._wht_account() if self.amount_withholding > 0 else None

        if not self.number:
            self.number = self.journal.next_entry_name()

        entry = JournalEntry.objects.create(
            tenant=self.tenant,
            journal=self.journal,
            date=self.date,
            ref=f'Vendor bill {self.number}'
                + (f' ({self.vendor_reference})' if self.vendor_reference else ''),
            partner=self.partner,
            notes=f'Posted from SupplierBill {self.number}',
        )

        # Dr Expense (per line)
        for line in self.lines.all():
            JournalEntryLine.objects.create(
                tenant=self.tenant, entry=entry, account=line.account, partner=self.partner,
                name=line.description[:64] or f'BILL {self.number}',
                debit=line.amount_untaxed, credit=Decimal('0'),
            )
        # Dr VAT recoverable
        if vat is not None:
            JournalEntryLine.objects.create(
                tenant=self.tenant, entry=entry, account=vat, partner=self.partner,
                name=f'VAT recov. {self.number}',
                debit=self.amount_tax, credit=Decimal('0'),
            )
        # Cr Payable (net of WHT)
        JournalEntryLine.objects.create(
            tenant=self.tenant, entry=entry, account=payable, partner=self.partner,
            name=f'BILL {self.number}',
            debit=Decimal('0'), credit=self.amount_net_payable,
        )
        # Cr WHT payable to govt
        if wht is not None:
            JournalEntryLine.objects.create(
                tenant=self.tenant, entry=entry, account=wht, partner=self.partner,
                name=f'WHT {self.number}',
                debit=Decimal('0'), credit=self.amount_withholding,
            )

        entry.post()
        self.journal_entry = entry
        self.state = 'posted'
        self.posted_at = timezone.now()
        self.save()

    @transaction.atomic
    def record_payment(self, bank_journal, payment_date=None):
        if self.state != 'posted':
            raise ValidationError('Only posted bills can be paid.')
        if not bank_journal.default_account_id:
            raise ValidationError(
                f'Journal {bank_journal.code} has no default account configured.',
            )
        if bank_journal.type not in ('bank', 'cash'):
            raise ValidationError('Payment journal must be of type bank or cash.')

        payable = self._payable_account()
        payment_date = payment_date or timezone.now().date()
        net = self.amount_net_payable

        entry = JournalEntry.objects.create(
            tenant=self.tenant,
            journal=bank_journal,
            date=payment_date,
            ref=f'Payment for {self.number}',
            partner=self.partner,
            notes=f'Payment recorded against SupplierBill {self.number}',
        )
        JournalEntryLine.objects.create(
            tenant=self.tenant, entry=entry, account=payable,
            partner=self.partner, name=f'Pay {self.number}',
            debit=net, credit=Decimal('0'),
        )
        JournalEntryLine.objects.create(
            tenant=self.tenant, entry=entry, account=bank_journal.default_account,
            partner=self.partner, name=f'Pay {self.number}',
            debit=Decimal('0'), credit=net,
        )
        entry.post()

        self.payment_entry = entry
        self.state = 'paid'
        self.paid_at = timezone.now()
        self.save()

    def cancel(self):
        if self.state != 'draft':
            raise ValidationError(
                'Only draft bills can be cancelled. '
                'Issue a debit note for posted bills.',
            )
        self.state = 'cancelled'
        self.save(update_fields=['state', 'updated_at'])


class SupplierBillLine(models.Model):
    """A single line on a supplier bill."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='supplier_bill_lines',
    )
    bill = models.ForeignKey(
        SupplierBill, on_delete=models.CASCADE, related_name='lines',
    )
    sequence = models.IntegerField(default=10)
    description = models.CharField(max_length=512)
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='+',
        limit_choices_to={'type__in': [
            'expense', 'expense_direct_cost', 'expense_other',
            'asset_current', 'asset_non_current', 'asset_fixed',
        ]},
        help_text='Expense or asset account being debited (e.g. 6xx in SYSCOHADA)',
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('1'))
    unit_price = models.DecimalField(max_digits=18, decimal_places=2)
    tax_rate = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal('0'),
        help_text='VAT percentage on this line',
    )

    objects = TenantManager()

    class Meta:
        ordering = ['bill', 'sequence', 'id']

    def __str__(self):
        return f'{self.description} ({self.amount})'

    @property
    def amount_untaxed(self):
        return (self.quantity * self.unit_price).quantize(Decimal('0.01'))

    @property
    def amount_tax(self):
        return (self.amount_untaxed * self.tax_rate / 100).quantize(Decimal('0.01'))

    @property
    def amount(self):
        return self.amount_untaxed + self.amount_tax


# ---------------------------------------------------------------------------
# Bank reconciliation (Phase 1.3)
# ---------------------------------------------------------------------------


class BankStatement(models.Model):
    """An imported bank statement (one upload = one statement).

    Workflow:
      1. User uploads a CSV → rows become BankStatementLine objects.
      2. For each line, user either:
         (a) MATCHES it to an existing posted JournalEntryLine on the
             same bank account (typically the bank side of a payment
             entry from an invoice or bill), reconciling both sides; or
         (b) POSTS a new JournalEntry inline (bank fee, transfer,
             cash deposit, etc.). Until the dedicated GL UI is built
             (Phase 1.4), this is done via the admin add view.
      3. State flips to ``reconciled`` once every line is matched
         or posted.
    """

    STATES = [
        ('draft', 'Draft'),
        ('in_progress', 'In progress'),
        ('reconciled', 'Reconciled'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='bank_statements',
    )
    journal = models.ForeignKey(
        Journal, on_delete=models.PROTECT, related_name='bank_statements',
        limit_choices_to={'type__in': ['bank', 'cash']},
        help_text='Bank or cash journal this statement covers',
    )
    period_start = models.DateField()
    period_end = models.DateField()
    opening_balance = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0'),
        help_text='Balance per the bank at start of period',
    )
    closing_balance = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0'),
        help_text='Balance per the bank at end of period',
    )
    state = models.CharField(max_length=16, choices=STATES, default='draft')
    notes = models.TextField(blank=True)

    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='+',
    )
    imported_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-period_end', '-id']

    def __str__(self):
        return f'{self.journal.code} {self.period_start} → {self.period_end}'

    @property
    def total_inflow(self):
        return self.lines.filter(amount__gt=0).aggregate(s=models.Sum('amount'))['s'] or Decimal('0')

    @property
    def total_outflow(self):
        return abs(self.lines.filter(amount__lt=0).aggregate(s=models.Sum('amount'))['s'] or Decimal('0'))

    @property
    def computed_closing(self):
        net = self.lines.aggregate(s=models.Sum('amount'))['s'] or Decimal('0')
        return self.opening_balance + net

    @property
    def reconciled_lines_count(self):
        return self.lines.exclude(state='unmatched').count()

    @property
    def total_lines_count(self):
        return self.lines.count()

    @property
    def is_fully_reconciled(self):
        return self.total_lines_count > 0 and self.reconciled_lines_count == self.total_lines_count

    def update_state(self):
        """Recompute state based on line statuses. Idempotent."""
        n_total = self.total_lines_count
        n_done = self.reconciled_lines_count
        if n_total == 0 or n_done == 0:
            new_state = 'draft'
        elif n_done < n_total:
            new_state = 'in_progress'
        else:
            new_state = 'reconciled'
        if new_state != self.state:
            self.state = new_state
            self.save(update_fields=['state', 'updated_at'])


class BankStatementLine(models.Model):
    """A single row from an imported bank statement."""

    STATES = [
        ('unmatched', 'Unmatched'),
        ('matched', 'Matched'),
        ('posted', 'Posted'),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='bank_statement_lines',
    )
    statement = models.ForeignKey(
        BankStatement, on_delete=models.CASCADE, related_name='lines',
    )
    sequence = models.IntegerField(default=10)
    transaction_date = models.DateField()
    value_date = models.DateField(null=True, blank=True)
    description = models.CharField(max_length=512)
    reference = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='Signed: positive = inflow, negative = outflow',
    )
    state = models.CharField(max_length=16, choices=STATES, default='unmatched')

    matched_entry_line = models.ForeignKey(
        JournalEntryLine, on_delete=models.PROTECT, null=True, blank=True,
        related_name='matched_bank_lines',
    )
    generated_entry = models.ForeignKey(
        JournalEntry, on_delete=models.PROTECT, null=True, blank=True,
        related_name='generated_from_bank_lines',
    )

    matched_at = models.DateTimeField(null=True, blank=True)
    matched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='+',
    )

    objects = TenantManager()

    class Meta:
        ordering = ['statement', 'sequence', 'transaction_date', 'id']

    def __str__(self):
        sign = '+' if self.amount >= 0 else ''
        return f'{self.transaction_date} {sign}{self.amount} {self.description[:40]}'

    @property
    def is_inflow(self):
        return self.amount > 0

    @property
    def is_outflow(self):
        return self.amount < 0

    @property
    def abs_amount(self):
        return abs(self.amount)

    def candidate_entry_lines(self, *, days_window=7):
        """JournalEntryLine rows that could plausibly match this line:
        same bank account, matching magnitude on the correct side
        (inflow→debit, outflow→credit), within ±days_window of our date,
        not already matched to another statement line."""
        if not self.statement.journal.default_account_id:
            return JournalEntryLine.objects.none()
        bank_acct = self.statement.journal.default_account
        date_lo = self.transaction_date - timedelta(days=days_window)
        date_hi = self.transaction_date + timedelta(days=days_window)
        qs = (
            JournalEntryLine.objects.for_tenant(self.tenant)
            .select_related('entry', 'account', 'partner')
            .filter(
                account=bank_acct,
                entry__state='posted',
                entry__date__gte=date_lo,
                entry__date__lte=date_hi,
            )
            .exclude(matched_bank_lines__isnull=False)
            .order_by('entry__date', 'id')
        )
        if self.is_inflow:
            qs = qs.filter(debit=self.amount)
        else:
            qs = qs.filter(credit=self.abs_amount)
        return qs

    @transaction.atomic
    def match_to(self, entry_line, *, user=None):
        """Manually reconcile this line against an existing JE line."""
        if self.state != 'unmatched':
            raise ValidationError(f'Line is already {self.get_state_display().lower()}.')
        bank_acct = self.statement.journal.default_account
        if entry_line.account_id != bank_acct.id:
            raise ValidationError('Entry line is on a different account.')
        if self.is_inflow and entry_line.debit != self.amount:
            raise ValidationError('Amount mismatch (inflow vs entry-line debit).')
        if self.is_outflow and entry_line.credit != self.abs_amount:
            raise ValidationError('Amount mismatch (outflow vs entry-line credit).')
        if entry_line.matched_bank_lines.exclude(pk=self.pk).exists():
            raise ValidationError('Entry line is already reconciled with another bank line.')
        self.matched_entry_line = entry_line
        self.matched_at = timezone.now()
        self.matched_by = user
        self.state = 'matched'
        self.save()
        self.statement.update_state()

    @transaction.atomic
    def unmatch(self):
        if self.state == 'posted':
            raise ValidationError(
                'This line generated a journal entry; cancel that entry '
                'first if you want to revert reconciliation.'
            )
        if self.state == 'unmatched':
            return
        self.matched_entry_line = None
        self.matched_at = None
        self.matched_by = None
        self.state = 'unmatched'
        self.save()
        self.statement.update_state()


def parse_bank_csv(text, *, date_format='%Y-%m-%d'):
    """Parse a flexible bank-statement CSV into row dicts ready to create
    BankStatementLine objects.

    Recognised column names (case-insensitive, any order):
      - date / transaction date / tx date
      - description / memo / label / libellé
      - amount / value / montant (signed, negative = outflow)
      - reference / ref / référence
      - value date / value_date / date_valeur

    Auto-detects delimiter (, ; tab |). Returns a list of tuples
    ``(row_dict_or_None, error_str_or_None)`` so callers can show
    per-row parse errors instead of failing the whole import.
    """
    import csv
    import io
    from datetime import datetime

    if not text:
        return []
    sample = text[:1024]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return []
    cols = {name.strip().lower(): name for name in reader.fieldnames if name}

    def col(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    date_col = col('date', 'transaction date', 'tx date')
    desc_col = col('description', 'memo', 'label', 'libelle', 'libellé')
    amt_col = col('amount', 'value', 'montant')
    ref_col = col('reference', 'ref', 'référence')
    val_col = col('value date', 'value_date', 'date_valeur')

    if not (date_col and desc_col and amt_col):
        raise ValueError(
            'CSV must have columns for date, description, and amount '
            f'(found: {list(cols.keys())}).'
        )

    def _parse_date(raw):
        raw = (raw or '').strip()
        if not raw:
            return None
        for fmt in (date_format, '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y'):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"Couldn't parse date {raw!r}")

    out = []
    for raw in reader:
        try:
            d = _parse_date(raw.get(date_col))
            if d is None:
                raise ValueError('date is empty')
            a_raw = (raw.get(amt_col) or '').strip()
            a_norm = a_raw.replace(' ', '').replace(' ', '')
            if ',' in a_norm and '.' in a_norm:
                a_norm = a_norm.replace(',', '')
            elif ',' in a_norm:
                a_norm = a_norm.replace(',', '.')
            amt = Decimal(a_norm)
            vd = _parse_date(raw.get(val_col)) if val_col else None
            out.append((
                {
                    'transaction_date': d,
                    'value_date': vd,
                    'description': (raw.get(desc_col) or '').strip()[:512],
                    'reference': (raw.get(ref_col) or '').strip()[:64] if ref_col else '',
                    'amount': amt,
                },
                None,
            ))
        except Exception as exc:
            out.append((None, f'row {reader.line_num}: {exc}'))
    return out
