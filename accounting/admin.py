"""Django admin registration. This is the UI for the first draft."""

from django.contrib import admin, messages
from django.utils import timezone

from .models import (
    Account,
    Company,
    Currency,
    DepreciationLine,
    FixedAsset,
    Journal,
    JournalEntry,
    JournalEntryLine,
    Membership,
    Partner,
    Tenant,
)


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
class PartnerAdmin(admin.ModelAdmin):
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
class AccountAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'type', 'reconcile', 'deprecated')
    list_filter = ('type', 'reconcile', 'deprecated', 'currency')
    search_fields = ('code', 'name')
    ordering = ('code',)
    list_per_page = 50


@admin.register(Journal)
class JournalAdmin(admin.ModelAdmin):
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
class JournalEntryAdmin(admin.ModelAdmin):
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
class JournalEntryLineAdmin(admin.ModelAdmin):
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
class FixedAssetAdmin(admin.ModelAdmin):
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
class DepreciationLineAdmin(admin.ModelAdmin):
    list_display = ('asset', 'period_date', 'amount', 'posted', 'journal_entry')
    list_filter = ('posted', 'period_date')
    search_fields = ('asset__code', 'asset__name')
    list_select_related = ('asset', 'journal_entry')
