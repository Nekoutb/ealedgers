"""Forms for the tenant-procedure UI (Step 24).

A ``TenantProcedure`` is a tenant's own rule that specialises a framework
default (e.g. "we depreciate vehicles over 4 years"). This form is the
self-serve surface for creating/editing one. The structured
``trigger_conditions`` / ``effects`` are the same loose-JSON DSL the rules
use; the framework-conflict validator (Step 25) runs on save and sets
``validation_status`` — this form always leaves a saved procedure in the
``pending`` state for that check to pick up.
"""

from django import forms
from django.utils.text import slugify

from .models import Rule, TenantProcedure


def unique_procedure_slug(tenant, title, exclude_pk=None):
    """A slug unique within ``tenant`` (the model's uniqueness scope)."""
    base = slugify(title) or "procedure"
    slug = base
    n = 2
    qs = TenantProcedure.objects.for_tenant(tenant)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    while qs.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
        if n > 9999:  # defensive — should never happen
            raise ValueError("Could not generate a unique procedure slug")
    return slug


class TenantProcedureForm(forms.ModelForm):
    """Create / edit a tenant procedure. ``tenant`` is supplied by the view
    (never user-controlled) and used to scope the slug and ownership."""

    class Meta:
        model = TenantProcedure
        fields = ["title", "description", "overrides_rule",
                  "trigger_conditions", "effects", "active"]
        widgets = {
            "title": forms.TextInput(attrs={
                "placeholder": "e.g. Vehicles depreciate over 4 years",
                "autofocus": True,
            }),
            "description": forms.Textarea(attrs={
                "rows": 3,
                "placeholder": "Plain statement of the procedure, e.g. "
                               "“Company vehicles are depreciated "
                               "straight-line over 4 years (48 months).”",
            }),
            "trigger_conditions": forms.Textarea(attrs={
                "rows": 4, "class": "kp-json",
                "placeholder": '{"asset_class": "vehicles"}',
            }),
            "effects": forms.Textarea(attrs={
                "rows": 4, "class": "kp-json",
                "placeholder": '{"method": "straight_line", '
                               '"useful_life_months": 48}',
            }),
        }

    def __init__(self, *args, tenant=None, **kwargs):
        self.tenant = tenant
        super().__init__(*args, **kwargs)
        self.fields["description"].required = True
        self.fields["overrides_rule"].required = False
        self.fields["overrides_rule"].queryset = (
            Rule.objects.filter(active=True).order_by("framework", "slug")
        )
        self.fields["overrides_rule"].label = \
            "Specialises which framework rule? (optional)"
        self.fields["trigger_conditions"].required = False
        self.fields["effects"].required = False
        self.fields["trigger_conditions"].help_text = \
            "Optional JSON — when this procedure applies."
        self.fields["effects"].help_text = \
            "Optional JSON — what it does."

    def clean_trigger_conditions(self):
        # forms.JSONField already rejected malformed JSON; coerce empty → {}.
        return self.cleaned_data.get("trigger_conditions") or {}

    def clean_effects(self):
        return self.cleaned_data.get("effects") or {}
