"""Knowledge-layer admin (Step 13).

Rule + Citation are global (plain ModelAdmin). TenantProcedure is
tenant-scoped, so it uses the same TenantAwareAdmin mixin as the rest of
the platform. The richer rule-explorer UI (browse / search / cite-from)
lands in Step 23; this is the data-entry surface.
"""

from django.contrib import admin

from accounting.admin import TenantAwareAdmin

from .models import Citation, Rule, TenantProcedure


class CitationInline(admin.TabularInline):
    model = Citation
    extra = 0
    fields = ("sequence", "reference", "text", "page", "url")


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = ("slug", "framework", "scope", "knowledge_slice",
                    "title", "review_status", "active",
                    "effective_from", "effective_to")
    list_filter = ("framework", "scope", "review_status", "active",
                   "knowledge_slice", "jurisdiction")
    search_fields = ("slug", "title", "source_ref", "source_text")
    readonly_fields = ("created_at", "updated_at", "created_by")
    inlines = [CitationInline]
    fieldsets = (
        (None, {"fields": ("slug", "title", "scope", "framework",
                           "knowledge_slice", "jurisdiction", "active")}),
        ("Effectivity", {"fields": ("effective_from", "effective_to")}),
        ("Source", {"fields": ("source_ref", "source_text")}),
        ("Rule logic", {"fields": ("trigger_conditions", "effects",
                                   "examples", "confidence_floor")}),
        ("QA", {"fields": ("review_status",)}),
        ("Audit", {"classes": ("collapse",),
                   "fields": ("created_by", "created_at", "updated_at")}),
    )

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Citation)
class CitationAdmin(admin.ModelAdmin):
    list_display = ("rule", "sequence", "reference", "page")
    search_fields = ("reference", "text", "rule__slug")
    list_select_related = ("rule",)


@admin.register(TenantProcedure)
class TenantProcedureAdmin(TenantAwareAdmin):
    list_display = ("slug", "title", "tenant", "validation_status",
                    "overrides_rule", "active", "updated_at")
    list_filter = ("validation_status", "active")
    search_fields = ("slug", "title", "description")
    readonly_fields = ("validation_status", "validation_notes",
                       "created_at", "updated_at", "created_by")
    autocomplete_fields = ("overrides_rule",)
    fieldsets = (
        (None, {"fields": ("slug", "title", "description", "active")}),
        ("Rule logic", {"fields": ("trigger_conditions", "effects",
                                   "overrides_rule")}),
        ("Validation", {"fields": ("validation_status", "validation_notes"),
                        "description": "Set by the framework-conflict "
                                       "validator (Step 25)."}),
        ("Audit", {"classes": ("collapse",),
                   "fields": ("created_by", "created_at", "updated_at")}),
    )

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
