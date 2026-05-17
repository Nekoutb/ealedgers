"""Django admin registration. This is the UI for the first draft."""

from django.contrib import admin, messages
from django.utils import timezone

from .models import (
    Account,
    Company,
    CustomerInvoice,
    CustomerInvoiceLine,
    Currency,
    DepreciationLine,
    FixedAsset,
    Journal,
    JournalEntry,
    JournalEntryLine,
    Membership,
    Partner,
    SupplierBill,
    SupplierBillLine,
    Tenant,
)


# ---------------------------------------------------------------------------
# Tenant-aware admin mixin
# ---------------------------------------------------------------------------


class TenantAwareAdmin(admin.ModelAdmin):
    """Mixin for ModelAdmins of tenant-scoped models.

    - List view is filtered to ``request.tenant``.
    - On add, ``obj.tenant`` is auto-set to ``request.tenant`` if not provided.
    - FK selectors only show records belonging to ``request.tenant``
      (so e.g. the account-picker on a JournalEntry line doesn't leak other
      tenants' accounts).
    """

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            # No tenant context (e.g. anon, or user with no memberships) →
            # show nothing. Superuser without a tenant context falls here too
            # — by design, they should switch tenant first.
            return qs.none()
        return qs.filter(tenant=tenant)

    def save_model(self, request, obj, form, change):
        if not change and not getattr(obj, "tenant_id", None):
            tenant = getattr(request, "tenant", None)
            if tenant is not None:
                obj.tenant = tenant
        super().save_model(request, obj, form, change)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        tenant = getattr(request, "tenant", None)
        if tenant is not None:
            related = db_field.related_model
            # Filter FK selectors when the target model itself is tenant-scoped.
            if related is not None and hasattr(related, "_meta") and any(
                f.name == "tenant" for f in related._meta.fields
            ):
                kwargs["queryset"] = related.objects.filter(tenant=tenant)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    autocomplete_fields = ("user",)
    fields = ("user", "role", "active", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "country", "business_type", "currency", "plan", "owner", "active")
    list_filter = ("business_type", "plan", "active", "country")
    search_fields = ("slug", "name", "legal_name", "tax_id", "company_registry")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("owner", "currency")
    inlines = [MembershipInline]
    fieldsets = (
        (None, {"fields": ("slug", "name", "legal_name", "business_type", "plan", "active")}),
        ("Locale & accounting", {"fields": ("country", "currency", "fiscal_year_start_month")}),
        ("Identification", {"fields": ("tax_id", "company_registry")}),
        ("Ownership", {"fields": ("owner",)}),
        ("Audit", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "tenant", "role", "active", "created_at")
    list_filter = ("role", "active", "tenant")
    search_fields = ("user__username", "user__email", "tenant__slug", "tenant__name")
    autocomplete_fields = ("user", "tenant")
    readonly_fields = ("created_at",)


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'symbol', 'decimal_places', 'active')
    list_filter = ('active',)
    search_fields = ('code', 'name')


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('name', 'legal_name', 'currency', 'country', 'active')
    list_filter = ('active', 'country')
    search_fields = ('name', 'legal_name', 'tax_id', 'company_registry')


@admin.register(Partner)
class PartnerAdmin(TenantAwareAdmin):
    list_display = ('name', 'partner_type', 'tax_id', 'city', 'country', 'active')
    list_filter = ('partner_type', 'is_company', 'active', 'country')
    search_fields = ('name', 'tax_id', 'email', 'company_registry')
    fieldsets = (
        (None, {'fields': ('name', 'partner_type', 'is_company', 'active')}),
        ('Identification', {'fields': ('tax_id', 'company_registry')}),
        ('Address', {'fields': ('street', 'street2', 'city', 'zip', 'country')}),
        ('Contact', {'fields': ('email', 'phone', 'website')}),
        ('Accounting', {'fields': ('account_receivable', 'account_payable', 'credit_limit')}),
        ('Notes', {'fields': ('notes',)}),
    )


# ---------------------------------------------------------------------------
# Chart and journals
# ---------------------------------------------------------------------------


@admin.register(Account)
class AccountAdmin(TenantAwareAdmin):
    list_display = ('code', 'name', 'type', 'reconcile', 'deprecated')
    list_filter = ('type', 'reconcile', 'deprecated', 'currency')
    search_fields = ('code', 'name')
    ordering = ('code',)
    list_per_page = 50


@admin.register(Journal)
class JournalAdmin(TenantAwareAdmin):
    list_display = ('code', 'name', 'type', 'default_account', 'next_sequence', 'active')
    list_filter = ('type', 'active')
    search_fields = ('code', 'name')
    autocomplete_fields = ('default_account',)


# ---------------------------------------------------------------------------
# Journal entries — the core double-entry UI
# ---------------------------------------------------------------------------


class JournalEntryLineInline(admin.TabularInline):
    model = JournalEntryLine
    extra = 2
    autocomplete_fields = ('account', 'partner')
    fields = ('account', 'partner', 'name', 'debit', 'credit')


@admin.register(JournalEntry)
class JournalEntryAdmin(TenantAwareAdmin):
    list_display = (
        'name_or_draft', 'date', 'journal', 'partner', 'state',
        'total_debit', 'total_credit', 'is_balanced_display',
    )
    list_filter = ('state', 'journal', 'date')
    search_fields = ('name', 'ref', 'partner__name')
    autocomplete_fields = ('partner',)
    inlines = [JournalEntryLineInline]
    actions = ['action_post', 'action_cancel']
    readonly_fields = ('name', 'created_at', 'updated_at', 'posted_at')
    fieldsets = (
        (None, {'fields': ('journal', 'date', 'ref', 'partner', 'notes')}),
        ('State', {'fields': ('state', 'name', 'posted_at')}),
        ('Audit', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    @admin.display(description='Entry', ordering='name')
    def name_or_draft(self, obj):
        return obj.name or f'(draft #{obj.id})'

    @admin.display(boolean=True, description='Balanced?')
    def is_balanced_display(self, obj):
        return obj.is_balanced

    @admin.action(description='Post selected (validate debits=credits, lock)')
    def action_post(self, request, queryset):
        posted = 0
        for entry in queryset:
            try:
                entry.post()
                posted += 1
            except Exception as e:
                self.message_user(request, f'{entry}: {e}', level=messages.ERROR)
        if posted:
            self.message_user(request, f'Posted {posted} entries.', level=messages.SUCCESS)

    @admin.action(description='Cancel selected (only drafts)')
    def action_cancel(self, request, queryset):
        count = 0
        for entry in queryset:
            try:
                entry.cancel()
                count += 1
            except Exception as e:
                self.message_user(request, f'{entry}: {e}', level=messages.ERROR)
        if count:
            self.message_user(request, f'Cancelled {count} entries.', level=messages.SUCCESS)


@admin.register(JournalEntryLine)
class JournalEntryLineAdmin(TenantAwareAdmin):
    """The AR/AP sub-ledger lives here. Filter by `account → type → Receivable`
    (or Payable) plus a partner to see that partner's open items."""

    list_display = (
        'entry', 'entry_date', 'account', 'partner',
        'name', 'debit', 'credit', 'reconciled', 'entry_state',
    )
    list_filter = ('entry__state', 'account__type', 'reconciled', 'account')
    search_fields = ('entry__name', 'name', 'partner__name', 'account__code', 'account__name')
    autocomplete_fields = ('entry', 'account', 'partner')
    list_select_related = ('entry', 'account', 'partner')

    @admin.display(description='Date', ordering='entry__date')
    def entry_date(self, obj):
        return obj.entry.date

    @admin.display(description='Entry state', ordering='entry__state')
    def entry_state(self, obj):
        return obj.entry.get_state_display()


# ---------------------------------------------------------------------------
# Fixed assets and depreciation
# ---------------------------------------------------------------------------


class DepreciationLineInline(admin.TabularInline):
    model = DepreciationLine
    extra = 0
    fields = ('period_date', 'amount', 'posted', 'journal_entry')
    readonly_fields = ('journal_entry',)
    can_delete = False
    ordering = ('period_date',)


@admin.register(FixedAsset)
class FixedAssetAdmin(TenantAwareAdmin):
    list_display = (
        'code', 'name', 'purchase_date', 'purchase_cost',
        'method', 'useful_life_months', 'book_value', 'state',
    )
    list_filter = ('state', 'method')
    search_fields = ('code', 'name')
    autocomplete_fields = (
        'asset_account',
        'accumulated_depreciation_account',
        'depreciation_expense_account',
        'depreciation_journal',
    )
    inlines = [DepreciationLineInline]
    actions = ['action_generate_schedule', 'action_post_due']
    fieldsets = (
        (None, {'fields': ('code', 'name', 'state', 'notes')}),
        ('Cost & life', {
            'fields': (
                'purchase_date', 'in_service_date', 'purchase_cost', 'salvage_value',
                'useful_life_months', 'method', 'declining_rate',
            )
        }),
        ('GL accounts', {
            'fields': (
                'asset_account',
                'accumulated_depreciation_account',
                'depreciation_expense_account',
                'depreciation_journal',
            )
        }),
    )

    @admin.action(description='(Re)generate depreciation schedule')
    def action_generate_schedule(self, request, queryset):
        for asset in queryset:
            asset.generate_schedule()
            if asset.state == 'draft':
                asset.state = 'in_use'
                asset.save(update_fields=['state'])
        self.message_user(
            request, f'Schedule generated for {queryset.count()} asset(s).', level=messages.SUCCESS
        )

    @admin.action(description='Post all depreciation due (up to today)')
    def action_post_due(self, request, queryset):
        today = timezone.now().date()
        total = 0
        for asset in queryset:
            total += asset.post_depreciation(today)
        self.message_user(
            request, f'Posted {total} depreciation entries.', level=messages.SUCCESS
        )


@admin.register(DepreciationLine)
class DepreciationLineAdmin(TenantAwareAdmin):
    list_display = ('asset', 'period_date', 'amount', 'posted', 'journal_entry')
    list_filter = ('posted', 'period_date')
    search_fields = ('asset__code', 'asset__name')
    list_select_related = ('asset', 'journal_entry')


# ---------------------------------------------------------------------------
# Customer invoicing (Phase 1.1)
# ---------------------------------------------------------------------------


class CustomerInvoiceLineInline(admin.TabularInline):
    model = CustomerInvoiceLine
    extra = 1
    fields = ('sequence', 'description', 'account', 'quantity', 'unit_price', 'tax_rate')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        tenant = getattr(request, 'tenant', None)
        if tenant is None:
            return qs.none()
        return qs.filter(tenant=tenant)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        tenant = getattr(request, 'tenant', None)
        if tenant is not None and db_field.name == 'account':
            kwargs['queryset'] = Account.objects.filter(
                tenant=tenant, type__in=['income', 'income_other'], deprecated=False,
            ).order_by('code')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_new(self, form, commit=True):
        """Inline-saved new line — set tenant from the parent invoice."""
        obj = super().save_new(form, commit=False)
        obj.tenant_id = obj.invoice.tenant_id
        if commit:
            obj.save()
        return obj


@admin.register(CustomerInvoice)
class CustomerInvoiceAdmin(TenantAwareAdmin):
    list_display = (
        'number_or_draft', 'date', 'partner', 'amount_total', 'amount_withholding',
        'state', 'due_date',
    )
    list_filter = ('state', 'date', 'journal')
    search_fields = ('number', 'partner__name', 'notes')
    list_select_related = ('partner', 'journal', 'currency')
    autocomplete_fields = ('partner',)
    readonly_fields = (
        'number', 'state', 'amount_subtotal', 'amount_tax', 'amount_withholding',
        'amount_total', 'journal_entry', 'payment_entry',
        'created_at', 'updated_at', 'posted_at', 'paid_at',
    )
    fieldsets = (
        (None, {
            'fields': ('number', 'partner', 'date', 'due_date', 'state'),
        }),
        ('Booking', {
            'fields': ('journal', 'currency', 'withholding_tax_rate'),
        }),
        ('Amounts', {
            'fields': ('amount_subtotal', 'amount_tax', 'amount_withholding', 'amount_total'),
        }),
        ('Audit', {
            'classes': ('collapse',),
            'fields': ('journal_entry', 'payment_entry',
                       'created_at', 'updated_at', 'posted_at', 'paid_at', 'notes'),
        }),
    )
    inlines = [CustomerInvoiceLineInline]
    actions = ['action_post', 'action_cancel']

    @admin.display(description='Number', ordering='number')
    def number_or_draft(self, obj):
        return obj.number or f'(draft #{obj.id})'

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Recompute totals after main row save — inline lines save afterwards
        # in Django's admin flow, so this catches any header-only edits
        # (changing WHT rate, for example).
        obj.recompute_amounts()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Now inline lines exist — recompute again.
        form.instance.recompute_amounts()

    @admin.action(description='Post selected draft invoices')
    def action_post(self, request, queryset):
        posted = 0
        errors = []
        for inv in queryset:
            try:
                inv.post()
                posted += 1
            except Exception as exc:  # ValidationError or anything from post()
                errors.append(f'{inv}: {exc}')
        if posted:
            self.message_user(
                request, f'Posted {posted} invoice(s).', level=messages.SUCCESS,
            )
        for err in errors:
            self.message_user(request, err, level=messages.ERROR)

    @admin.action(description='Cancel selected draft invoices')
    def action_cancel(self, request, queryset):
        cancelled = 0
        for inv in queryset.filter(state='draft'):
            inv.cancel()
            cancelled += 1
        self.message_user(
            request, f'Cancelled {cancelled} invoice(s).',
            level=messages.SUCCESS if cancelled else messages.WARNING,
        )


@admin.register(CustomerInvoiceLine)
class CustomerInvoiceLineAdmin(TenantAwareAdmin):
    list_display = ('invoice', 'sequence', 'description', 'quantity', 'unit_price',
                    'tax_rate', 'amount_untaxed_disp', 'amount_disp')
    list_filter = ('invoice__state',)
    search_fields = ('description', 'invoice__number')
    list_select_related = ('invoice', 'account')

    @admin.display(description='Subtotal')
    def amount_untaxed_disp(self, obj):
        return obj.amount_untaxed

    @admin.display(description='Total')
    def amount_disp(self, obj):
        return obj.amount


# ---------------------------------------------------------------------------
# Supplier bills (Phase 1.2) — mirror of CustomerInvoice admin
# ---------------------------------------------------------------------------


class SupplierBillLineInline(admin.TabularInline):
    model = SupplierBillLine
    extra = 1
    fields = ('sequence', 'description', 'account', 'quantity', 'unit_price', 'tax_rate')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        tenant = getattr(request, 'tenant', None)
        if tenant is None:
            return qs.none()
        return qs.filter(tenant=tenant)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        tenant = getattr(request, 'tenant', None)
        if tenant is not None and db_field.name == 'account':
            kwargs['queryset'] = Account.objects.filter(
                tenant=tenant,
                type__in=[
                    'expense', 'expense_direct_cost', 'expense_other',
                    'asset_current', 'asset_non_current', 'asset_fixed',
                ],
                deprecated=False,
            ).order_by('code')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_new(self, form, commit=True):
        obj = super().save_new(form, commit=False)
        obj.tenant_id = obj.bill.tenant_id
        if commit:
            obj.save()
        return obj


@admin.register(SupplierBill)
class SupplierBillAdmin(TenantAwareAdmin):
    list_display = (
        'number_or_draft', 'date', 'vendor_reference', 'partner', 'amount_total',
        'amount_withholding', 'state', 'due_date',
    )
    list_filter = ('state', 'date', 'journal')
    search_fields = ('number', 'vendor_reference', 'partner__name', 'notes')
    list_select_related = ('partner', 'journal', 'currency')
    autocomplete_fields = ('partner',)
    readonly_fields = (
        'number', 'state', 'amount_subtotal', 'amount_tax', 'amount_withholding',
        'amount_total', 'journal_entry', 'payment_entry',
        'created_at', 'updated_at', 'posted_at', 'paid_at',
    )
    fieldsets = (
        (None, {
            'fields': ('number', 'vendor_reference', 'partner', 'date', 'due_date', 'state'),
        }),
        ('Booking', {
            'fields': ('journal', 'currency', 'withholding_tax_rate'),
        }),
        ('Amounts', {
            'fields': ('amount_subtotal', 'amount_tax', 'amount_withholding', 'amount_total'),
        }),
        ('Audit', {
            'classes': ('collapse',),
            'fields': ('journal_entry', 'payment_entry',
                       'created_at', 'updated_at', 'posted_at', 'paid_at', 'notes'),
        }),
    )
    inlines = [SupplierBillLineInline]
    actions = ['action_post', 'action_cancel']

    @admin.display(description='Number', ordering='number')
    def number_or_draft(self, obj):
        return obj.number or f'(draft #{obj.id})'

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        obj.recompute_amounts()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recompute_amounts()

    @admin.action(description='Post selected draft bills')
    def action_post(self, request, queryset):
        posted = 0
        errors = []
        for b in queryset:
            try:
                b.post()
                posted += 1
            except Exception as exc:
                errors.append(f'{b}: {exc}')
        if posted:
            self.message_user(request, f'Posted {posted} bill(s).', level=messages.SUCCESS)
        for err in errors:
            self.message_user(request, err, level=messages.ERROR)

    @admin.action(description='Cancel selected draft bills')
    def action_cancel(self, request, queryset):
        cancelled = 0
        for b in queryset.filter(state='draft'):
            b.cancel()
            cancelled += 1
        self.message_user(
            request, f'Cancelled {cancelled} bill(s).',
            level=messages.SUCCESS if cancelled else messages.WARNING,
        )


@admin.register(SupplierBillLine)
class SupplierBillLineAdmin(TenantAwareAdmin):
    list_display = ('bill', 'sequence', 'description', 'quantity', 'unit_price',
                    'tax_rate', 'amount_untaxed_disp', 'amount_disp')
    list_filter = ('bill__state',)
    search_fields = ('description', 'bill__number', 'bill__vendor_reference')
    list_select_related = ('bill', 'account')

    @admin.display(description='Subtotal')
    def amount_untaxed_disp(self, obj):
        return obj.amount_untaxed

    @admin.display(description='Total')
    def amount_disp(self, obj):
        return obj.amount
