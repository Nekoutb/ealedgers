"""Django admin registration. This is the UI for the first draft."""

from django.contrib import admin, messages
from django.forms.models import BaseInlineFormSet
from django.utils import timezone

from .models import (
    Account,
    AgentRun,
    AgentToolCall,
    ApprovalQueueItem,
    BankStatement,
    BusEvent,
    BankStatementLine,
    Company,
    CustomerInvoice,
    CustomerInvoiceLine,
    Currency,
    DepreciationLine,
    ERPConnection,
    ERPOperation,
    FixedAsset,
    FxRate,
    Journal,
    JournalEntry,
    JournalEntryLine,
    Membership,
    Partner,
    Period,
    PeriodLock,
    Provenance,
    SupplierBill,
    SupplierBillLine,
    Tenant,
    TenantDepartmentSubscription,
)


# ---------------------------------------------------------------------------
# Tenant-aware admin mixin
# ---------------------------------------------------------------------------


class TenantPropagatingInlineFormSet(BaseInlineFormSet):
    """Inline formset that copies the parent's ``tenant_id`` onto each
    child instance at form construction time.

    Without this, models whose ``tenant`` FK is NOT NULL (which is all of
    our business models since Phase 0.2's tightening) fail ``full_clean``
    during admin inline validation — the parent has set ``tenant_id``
    (see ``TenantAwareAdmin.save_form``), but the inline forms construct
    fresh child instances without copying that FK over.
    """

    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        parent_tenant_id = getattr(self.instance, "tenant_id", None)
        child_has_tenant = any(
            f.name == "tenant" for f in form.instance._meta.fields
        )
        if parent_tenant_id and child_has_tenant and not getattr(form.instance, "tenant_id", None):
            form.instance.tenant_id = parent_tenant_id
        return form


class TenantAwareAdmin(admin.ModelAdmin):
    """Mixin for ModelAdmins of tenant-scoped models.

    - List view is filtered to ``request.tenant``.
    - On add, ``obj.tenant`` is set to ``request.tenant`` BEFORE inline
      validation runs (via ``save_form``), so inline-line saves can
      propagate the parent's ``tenant_id`` to children.
    - FK selectors only show records belonging to ``request.tenant``.
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

    def save_form(self, request, form, change):
        """Set parent.tenant_id EARLY — before inline formsets validate.

        Django's changeform_view runs in this order:
          1. parent form_validated = form.is_valid()
          2. new_object = self.save_form(request, form, change)
          3. formsets = self._create_formsets(request, new_object, change)
          4. all_valid(formsets)  ← inline lines validated here
          5. self.save_model(request, new_object, form, change)
          6. self.save_related(request, form, formsets, change)

        ``save_model`` (step 5) used to be where we set ``tenant``, but
        that's too late — by step 4 the inline lines have already failed
        ``full_clean`` because they can't see the parent's tenant.

        Setting it in ``save_form`` (step 2) means the instance has
        ``tenant_id`` by the time inline formsets are built.
        """
        obj = super().save_form(request, form, change)
        if not change and not getattr(obj, "tenant_id", None):
            tenant = getattr(request, "tenant", None)
            if tenant is not None and any(
                f.name == "tenant" for f in obj._meta.fields
            ):
                obj.tenant = tenant
        return obj

    def save_model(self, request, obj, form, change):
        # save_form has typically already done this, but keep as belt-and-braces
        # for code paths that hit save_model directly (e.g. bulk actions).
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


class TenantDepartmentSubscriptionInline(admin.TabularInline):
    """Per-tenant department staffing, editable right on the Tenant page.
    Tick the departments this tenant wants (AP only, AR only, or all)."""
    model = TenantDepartmentSubscription
    extra = 0
    autocomplete_fields = ("default_approver",)
    fields = ("department", "active", "default_approver", "auto_action_cap")


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "country", "business_type", "currency",
                    "plan", "accounting_framework", "agent_enabled",
                    "subscribed_dept_count", "owner", "active")
    list_filter = ("business_type", "plan", "accounting_framework",
                   "agent_enabled", "active", "country")
    search_fields = ("slug", "name", "legal_name", "tax_id", "company_registry")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("owner", "currency")
    inlines = [MembershipInline, TenantDepartmentSubscriptionInline]
    fieldsets = (
        (None, {"fields": ("slug", "name", "legal_name", "business_type", "plan", "active")}),
        ("Locale & accounting", {"fields": ("country", "currency",
                                            "fiscal_year_start_month",
                                            "accounting_framework")}),
        ("Agent", {"fields": ("agent_enabled",),
                   "description": "Master kill switch. When off, no department "
                                  "agent auto-acts for this tenant."}),
        ("Identification", {"fields": ("tax_id", "company_registry")}),
        ("Ownership", {"fields": ("owner",)}),
        ("Audit", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Depts")
    def subscribed_dept_count(self, obj):
        return obj.department_subscriptions.filter(active=True).count()


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "tenant", "role", "active", "created_at")
    list_filter = ("role", "active", "tenant")
    search_fields = ("user__username", "user__email", "tenant__slug", "tenant__name")
    autocomplete_fields = ("user", "tenant")
    readonly_fields = ("created_at",)


@admin.register(TenantDepartmentSubscription)
class TenantDepartmentSubscriptionAdmin(TenantAwareAdmin):
    list_display = ("tenant", "department", "active", "default_approver",
                    "auto_action_cap", "updated_at")
    list_filter = ("department", "active")
    search_fields = ("tenant__name", "tenant__slug")
    autocomplete_fields = ("default_approver",)
    list_select_related = ("tenant", "default_approver")


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
    formset = TenantPropagatingInlineFormSet

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        tenant = getattr(request, 'tenant', None)
        if tenant is None:
            return qs.none()
        return qs.filter(tenant=tenant)


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

    class Media:
        # Live debit/credit running totals + balance indicator on the
        # inline grid (see static file for behaviour).
        js = ('accounting/admin/journal_entry_running_balance.js',)
        css = {'all': ('accounting/admin/journal_entry_running_balance.css',)}

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Restrict the Journal selector to GENERAL / BANK / CASH journals.

        Sale and purchase journals are auto-fed by CustomerInvoice and
        SupplierBill respectively; allowing manual entries there risks
        double-counting and breaks the invoice/bill state machines'
        amount tracking. Users who really need to touch those journals
        should edit the invoice/bill directly.

        NOTE: we narrow the queryset on the returned ``formfield``, not via
        ``kwargs['queryset']``, because the parent ``TenantAwareAdmin``
        already overwrites ``kwargs['queryset']`` with a plain tenant
        filter — our type-narrowing would be lost otherwise.
        """
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        tenant = getattr(request, 'tenant', None)
        if db_field.name == 'journal' and tenant is not None and formfield is not None:
            formfield.queryset = Journal.objects.filter(
                tenant=tenant,
                type__in=('general', 'bank', 'cash'),
                active=True,
            ).order_by('code')
        return formfield

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
    formset = TenantPropagatingInlineFormSet

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
    formset = TenantPropagatingInlineFormSet

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


# ---------------------------------------------------------------------------
# Bank reconciliation (Phase 1.3)
# ---------------------------------------------------------------------------


class BankStatementLineInline(admin.TabularInline):
    model = BankStatementLine
    extra = 0
    fields = ('sequence', 'transaction_date', 'description', 'reference',
              'amount', 'state', 'matched_entry_line', 'generated_entry')
    readonly_fields = ('state', 'matched_entry_line', 'generated_entry')
    formset = TenantPropagatingInlineFormSet

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        tenant = getattr(request, 'tenant', None)
        if tenant is None:
            return qs.none()
        return qs.filter(tenant=tenant)


@admin.register(BankStatement)
class BankStatementAdmin(TenantAwareAdmin):
    list_display = ('__str__', 'journal', 'period_start', 'period_end',
                    'opening_balance', 'closing_balance', 'state',
                    'reconciled_lines_count', 'total_lines_count')
    list_filter = ('state', 'journal')
    readonly_fields = ('state', 'imported_by', 'imported_at', 'updated_at')
    fieldsets = (
        (None, {'fields': ('journal', 'period_start', 'period_end',
                           'opening_balance', 'closing_balance', 'state', 'notes')}),
        ('Audit', {'classes': ('collapse',),
                   'fields': ('imported_by', 'imported_at', 'updated_at')}),
    )
    inlines = [BankStatementLineInline]

    def save_model(self, request, obj, form, change):
        if not change and not obj.imported_by_id:
            obj.imported_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(BankStatementLine)
class BankStatementLineAdmin(TenantAwareAdmin):
    list_display = ('transaction_date', 'description', 'amount', 'state',
                    'statement', 'matched_entry_line', 'generated_entry')
    list_filter = ('state', 'statement__journal')
    search_fields = ('description', 'reference')
    list_select_related = ('statement', 'matched_entry_line', 'generated_entry')


# ---------------------------------------------------------------------------
# Step 6 — Period / PeriodLock / FxRate
# ---------------------------------------------------------------------------


class PeriodLockInline(admin.TabularInline):
    """Audit-log of lock/unlock events shown read-only under each Period."""
    model = PeriodLock
    extra = 0
    fields = ('action', 'acted_at', 'acted_by', 'reason')
    readonly_fields = ('action', 'acted_at', 'acted_by', 'reason')
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Period)
class PeriodAdmin(TenantAwareAdmin):
    list_display = ('code', 'start_date', 'end_date', 'state', 'closed_at', 'updated_at')
    list_filter = ('state',)
    search_fields = ('code', 'notes')
    readonly_fields = ('state', 'closed_at', 'created_at', 'updated_at')
    fieldsets = (
        (None, {'fields': ('code', 'start_date', 'end_date', 'state', 'notes')}),
        ('Audit', {
            'classes': ('collapse',),
            'fields': ('closed_at', 'created_at', 'updated_at'),
        }),
    )
    inlines = [PeriodLockInline]
    actions = ['action_lock', 'action_unlock', 'action_start_close']

    @admin.action(description='Lock selected periods (no more posting)')
    def action_lock(self, request, queryset):
        n = 0
        for p in queryset:
            p.lock(user=request.user, reason='Bulk lock from admin')
            n += 1
        self.message_user(request, f'Locked {n} period(s).', level=messages.SUCCESS)

    @admin.action(description='Unlock selected periods')
    def action_unlock(self, request, queryset):
        n = 0
        for p in queryset:
            p.unlock(user=request.user, reason='Bulk unlock from admin')
            n += 1
        self.message_user(request, f'Unlocked {n} period(s).', level=messages.SUCCESS)

    @admin.action(description='Mark selected periods as close-in-progress')
    def action_start_close(self, request, queryset):
        n = 0
        for p in queryset:
            try:
                p.start_close(user=request.user)
                n += 1
            except Exception as exc:
                self.message_user(request, f'{p}: {exc}', level=messages.ERROR)
        self.message_user(request, f'Started close on {n} period(s).',
                          level=messages.SUCCESS if n else messages.WARNING)


@admin.register(PeriodLock)
class PeriodLockAdmin(TenantAwareAdmin):
    list_display = ('period', 'action', 'acted_at', 'acted_by', 'reason')
    list_filter = ('action',)
    search_fields = ('period__code', 'reason')
    readonly_fields = ('period', 'action', 'acted_at', 'acted_by', 'reason')
    list_select_related = ('period', 'acted_by')

    def has_add_permission(self, request):
        # PeriodLock rows are created only via Period.lock()/unlock(); never
        # added manually.
        return False


@admin.register(FxRate)
class FxRateAdmin(admin.ModelAdmin):
    """Global FX time-series. Plain ModelAdmin, not TenantAwareAdmin, because
    FxRate is intentionally not tenant-scoped (universal market data)."""
    list_display = ('base_currency', 'quote_currency', 'fixing_date', 'rate', 'source')
    list_filter = ('base_currency', 'quote_currency', 'source')
    search_fields = ('source', 'notes')
    date_hierarchy = 'fixing_date'
    list_select_related = ('base_currency', 'quote_currency')


# ---------------------------------------------------------------------------
# Step 7 — Provenance / AgentRun / AgentToolCall / ERPConnection / ERPOperation
# ---------------------------------------------------------------------------
#
# These are append-only audit/runtime tables. The admin treats them as
# mostly read-only: humans can browse, can't add or delete (except for
# ERPConnection, which IS a user-managed config record).


class AgentToolCallInline(admin.TabularInline):
    model = AgentToolCall
    extra = 0
    fields = ('sequence', 'tool', 'status', 'started_at', 'completed_at')
    readonly_fields = ('sequence', 'tool', 'status', 'started_at', 'completed_at')
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AgentRun)
class AgentRunAdmin(TenantAwareAdmin):
    list_display = ('department', 'task', 'status', 'confidence', 'llm_model',
                    'total_tokens', 'started_at', 'completed_at')
    list_filter = ('department', 'status', 'llm_model')
    search_fields = ('task', 'chain_id', 'error')
    readonly_fields = ('department', 'task', 'chain_id', 'status', 'llm_model',
                       'input_tokens', 'output_tokens', 'confidence',
                       'input_summary', 'output_summary', 'error',
                       'started_at', 'completed_at',
                       'reviewed_by', 'reviewed_at')
    fieldsets = (
        (None, {'fields': ('department', 'task', 'chain_id', 'status', 'confidence')}),
        ('Cost', {'fields': ('llm_model', 'input_tokens', 'output_tokens')}),
        ('I/O', {'fields': ('input_summary', 'output_summary', 'error'),
                 'classes': ('collapse',)}),
        ('Audit', {'fields': ('started_at', 'completed_at',
                              'reviewed_by', 'reviewed_at'),
                   'classes': ('collapse',)}),
    )
    inlines = [AgentToolCallInline]
    date_hierarchy = 'started_at'

    def has_add_permission(self, request):
        return False  # AgentRun rows are created by the agent runtime, not humans

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AgentToolCall)
class AgentToolCallAdmin(TenantAwareAdmin):
    list_display = ('agent_run', 'sequence', 'tool', 'status',
                    'started_at', 'completed_at')
    list_filter = ('status', 'tool')
    search_fields = ('tool', 'error', 'agent_run__task')
    readonly_fields = ('tenant', 'agent_run', 'sequence', 'tool',
                       'arguments', 'result', 'status', 'error',
                       'started_at', 'completed_at')
    list_select_related = ('agent_run',)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ERPConnection)
class ERPConnectionAdmin(TenantAwareAdmin):
    """User-managed: tenants configure their own ERP connections here."""
    list_display = ('name', 'vendor', 'version', 'health',
                    'is_primary', 'is_active', 'last_healthcheck_at')
    list_filter = ('vendor', 'health', 'is_active', 'is_primary')
    search_fields = ('name', 'last_healthcheck_error')
    readonly_fields = ('capabilities', 'health', 'last_healthcheck_at',
                       'last_healthcheck_error', 'created_at', 'updated_at')
    fieldsets = (
        (None, {'fields': ('name', 'vendor', 'version', 'is_primary', 'is_active')}),
        ('Routing', {'fields': ('config',),
                     'description': 'URL, ports, db name, env-var names — '
                                    'never put actual secrets here.'}),
        ('Capabilities', {'fields': ('capabilities',),
                          'description': 'Auto-populated by the connector at '
                                         'each healthcheck.'}),
        ('Health', {'fields': ('health', 'last_healthcheck_at',
                               'last_healthcheck_error'),
                    'classes': ('collapse',)}),
        ('Audit', {'fields': ('created_at', 'updated_at'),
                   'classes': ('collapse',)}),
    )


@admin.register(ERPOperation)
class ERPOperationAdmin(TenantAwareAdmin):
    list_display = ('capability', 'method', 'connection', 'status',
                    'retry_count', 'started_at', 'completed_at')
    list_filter = ('status', 'capability', 'connection')
    search_fields = ('method', 'error', 'capability')
    readonly_fields = ('tenant', 'connection', 'capability', 'method',
                       'tool_call', 'journal_entry',
                       'request', 'response', 'external_ids',
                       'status', 'error', 'retry_count',
                       'started_at', 'completed_at')
    list_select_related = ('connection', 'journal_entry')
    date_hierarchy = 'started_at'

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Provenance)
class ProvenanceAdmin(TenantAwareAdmin):
    list_display = ('source', 'summary_short', 'chain_id',
                    'journal_entry', 'approved_by', 'created_at')
    list_filter = ('source',)
    search_fields = ('summary', 'chain_id')
    readonly_fields = ('tenant', 'journal_entry', 'source', 'chain_id',
                       'summary', 'agent_run', 'approved_by', 'approved_at',
                       'citations', 'extra', 'created_at')
    list_select_related = ('journal_entry', 'agent_run', 'approved_by')
    date_hierarchy = 'created_at'

    @admin.display(description='Summary')
    def summary_short(self, obj):
        if not obj.summary:
            return '(no summary)'
        return obj.summary[:60] + ('…' if len(obj.summary) > 60 else '')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------------------
# Step 42 — Approval queue
# ---------------------------------------------------------------------------

def _approve_items(modeladmin, request, queryset):
    """Admin action: approve all selected pending items."""
    count = 0
    for item in queryset.filter(status='pending'):
        item.approve(user=request.user, note='Bulk-approved via admin action.')
        count += 1
    modeladmin.message_user(
        request,
        f'{count} item(s) approved.',
        level=messages.SUCCESS,
    )


_approve_items.short_description = 'Approve selected pending items'


def _reject_items(modeladmin, request, queryset):
    """Admin action: reject all selected pending items."""
    count = 0
    for item in queryset.filter(status='pending'):
        item.reject(user=request.user, note='Bulk-rejected via admin action.')
        count += 1
    modeladmin.message_user(
        request,
        f'{count} item(s) rejected.',
        level=messages.WARNING,
    )


_reject_items.short_description = 'Reject selected pending items'


@admin.register(ApprovalQueueItem)
class ApprovalQueueItemAdmin(TenantAwareAdmin):
    list_display = (
        'dept_code', 'action_short', 'status_badge',
        'chain_id_short', 'reviewed_by', 'created_at',
    )
    list_filter = ('dept_code', 'status')
    search_fields = ('action', 'chain_id', 'review_note')
    readonly_fields = (
        'tenant', 'dept_code', 'action', 'inputs', 'specialist_results',
        'chain_id', 'metadata', 'created_at',
    )
    list_select_related = ('reviewed_by',)
    date_hierarchy = 'created_at'
    actions = [_approve_items, _reject_items]

    fieldsets = (
        (None, {
            'fields': ('tenant', 'dept_code', 'action', 'chain_id'),
        }),
        ('Proposal details', {
            'fields': ('inputs', 'specialist_results', 'metadata'),
            'classes': ('collapse',),
        }),
        ('Review', {
            'fields': ('status', 'reviewed_by', 'review_note', 'reviewed_at'),
        }),
        ('Audit', {
            'fields': ('created_at',),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Action')
    def action_short(self, obj):
        return obj.action[:70] + ('…' if len(obj.action) > 70 else '')

    @admin.display(description='Chain ID')
    def chain_id_short(self, obj):
        return obj.chain_id[:12] + '…' if len(obj.chain_id) > 12 else obj.chain_id

    @admin.display(description='Status')
    def status_badge(self, obj):
        colours = {
            'pending':          '#fef3c7;color:#92400e',
            'approved':         '#d1fae5;color:#065f46',
            'auto_approved':    '#d1fae5;color:#065f46',
            'rejected':         '#fee2e2;color:#991b1b',
            'escalated':        '#fef3c7;color:#78350f',
            'executed':         '#dcfce7;color:#166534',
            'execution_failed': '#fee2e2;color:#991b1b',
        }
        style = colours.get(obj.status, '#f5f5f5;color:#525252')
        from django.utils.html import format_html
        return format_html(
            '<span style="background:{};padding:2px 8px;border-radius:999px;'
            'font-size:0.72rem;font-weight:600;white-space:nowrap">{}</span>',
            style, obj.get_status_display(),
        )

    def has_add_permission(self, request):
        return False     # items enter via from_proposal() only

    def has_delete_permission(self, request, obj=None):
        return False     # append-only — never delete


# ---------------------------------------------------------------------------
# Step 43 — Event bus
# ---------------------------------------------------------------------------

@admin.register(BusEvent)
class BusEventAdmin(TenantAwareAdmin):
    """Read-only view of every event emitted on the event bus for this tenant."""

    list_display = (
        'event_type', 'status_badge', 'handler_count',
        'chain_id_short', 'created_at', 'dispatched_at',
    )
    list_filter = ('status', 'event_type')
    search_fields = ('event_type', 'chain_id', 'error', 'task_id')
    readonly_fields = (
        'tenant', 'event_type', 'payload', 'chain_id', 'metadata',
        'status', 'handler_count', 'error', 'task_id',
        'created_at', 'dispatched_at',
    )
    list_select_related = ('tenant',)
    date_hierarchy = 'created_at'

    fieldsets = (
        (None, {
            'fields': ('tenant', 'event_type', 'status', 'chain_id'),
        }),
        ('Payload', {
            'fields': ('payload', 'metadata'),
            'classes': ('collapse',),
        }),
        ('Dispatch', {
            'fields': ('handler_count', 'task_id', 'error'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'dispatched_at'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Status')
    def status_badge(self, obj):
        colours = {
            'queued':     '#fef3c7;color:#92400e',
            'dispatched': '#d1fae5;color:#065f46',
            'failed':     '#fee2e2;color:#991b1b',
        }
        style = colours.get(obj.status, '#f5f5f5;color:#525252')
        from django.utils.html import format_html
        return format_html(
            '<span style="background:{};padding:2px 8px;border-radius:999px;'
            'font-size:0.72rem;font-weight:600;white-space:nowrap">{}</span>',
            style, obj.get_status_display(),
        )

    @admin.display(description='Chain ID')
    def chain_id_short(self, obj):
        if not obj.chain_id:
            return '—'
        return obj.chain_id[:12] + '…' if len(obj.chain_id) > 12 else obj.chain_id

    def has_add_permission(self, request):
        return False   # BusEvents are created only via agents.events.emit()

    def has_delete_permission(self, request, obj=None):
        return False   # append-only — never delete
