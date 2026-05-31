"""Knowledge-layer models (Step 13).

The versioned, sourced, retrievable rule base that the agents reason over.
Three models here:

  - Rule            — a GLOBAL framework / tax-code rule (SYSCOHADA, CGI,
                      IFRS, …). Shipped with the product, shared by all
                      tenants. Not tenant-scoped.
  - Citation        — an additional source passage backing a Rule (a rule
                      may cite more than one source). Optional; the Rule
                      also carries a primary source_ref + source_text inline.
  - TenantProcedure — a TENANT'S OWN rule, overriding / specialising a
                      framework default. Tenant-scoped. The validator
                      (Step 25) checks it doesn't violate the framework.

RuleVector (pgvector embedding of source_text) is deferred to after the
SSH bundle installs the pgvector extension — see docs/EXECUTION_PLAN.md
§7 "SSH bundle scheduling notes" and the Step-13 split note. Until then
retrieval (Step 14) uses Postgres full-text / trigram, and the vector
index is added on top.

The structured DSL in ``trigger_conditions`` / ``effects`` is intentionally
loose JSON for now; a schema is enforced when the proposer agent consumes
it (Phase P05).
"""

from django.conf import settings
from django.db import models

from accounting.managers import TenantManager


# A rule's scope decides which "kind" of knowledge it is.
RULE_SCOPES = [
    ("framework", "Accounting framework"),   # SYSCOHADA, IFRS …
    ("tax_code", "Tax code"),                # CGI Cameroun, etc.
]

# Knowledge-slice IDs from docs/EXECUTION_PLAN.md §3 (K01–K20). Free text so
# new slices don't need a migration, but documented here for grep-ability.
# e.g. "K01", "K11", "K10".


class Rule(models.Model):
    """One structured, sourced rule from an accounting framework or tax code.

    GLOBAL — not tenant-scoped. Distributed with the product. A tenant's
    overrides live in ``TenantProcedure``.
    """

    slug = models.SlugField(
        max_length=96, unique=True,
        help_text="Stable id, e.g. 'syscohada-coa-class-2' or "
                  "'cgi-2025-tva-deductible'.",
    )
    scope = models.CharField(max_length=16, choices=RULE_SCOPES)

    # Where the rule applies.
    jurisdiction = models.CharField(
        max_length=32, blank=True,
        help_text="e.g. 'OHADA', 'CMR', 'IFRS-global', or '' for N/A.",
    )
    framework = models.CharField(
        max_length=32,
        help_text="Framework/code + version, e.g. 'SYSCOHADA-2017', "
                  "'CGI-2025', 'IFRS'.",
    )
    knowledge_slice = models.CharField(
        max_length=8, blank=True, db_index=True,
        help_text="Encoding-catalogue slice id (K01–K20) for tracking.",
    )

    title = models.CharField(max_length=256)

    # Finance-law cycles: a rule may be valid only for a date range.
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)

    # Primary citation (the common single-source case).
    source_ref = models.CharField(
        max_length=256, blank=True,
        help_text="Short citation, e.g. 'SYSCOHADA Titre VII Ch.2 §41' or "
                  "'CGI 2025 art. 7'.",
    )
    source_text = models.TextField(
        blank=True, help_text="Full text of the cited passage.",
    )

    # Structured DSL — loose JSON for now (schema enforced by the proposer).
    trigger_conditions = models.JSONField(
        default=dict, blank=True,
        help_text="When does this rule apply? "
                  "e.g. {'transaction_type': 'fixed_asset_acquisition', "
                  "'asset_class': 'vehicles'}",
    )
    effects = models.JSONField(
        default=dict, blank=True,
        help_text="What does it do? e.g. {'depreciation_method': "
                  "'straight_line', 'useful_life_months': 48}",
    )

    # Few-shot examples for the LLM (list of {input, output} objects).
    examples = models.JSONField(default=list, blank=True)

    # Minimum agent confidence to apply this rule without human review.
    confidence_floor = models.DecimalField(
        max_digits=4, decimal_places=3, default=0.900,
    )

    # Encoding QA: high-stakes rules stay 'needs_review' until a human
    # framework expert signs off (the Step-27 gate).
    REVIEW_STATES = [
        ("needs_review", "Needs expert review"),
        ("verified", "Verified by expert"),
    ]
    review_status = models.CharField(
        max_length=16, choices=REVIEW_STATES, default="needs_review",
    )

    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["framework", "knowledge_slice", "slug"]
        indexes = [
            models.Index(fields=["framework", "scope"]),
            models.Index(fields=["knowledge_slice"]),
        ]

    def __str__(self):
        return f"[{self.framework}] {self.title}"

    @property
    def is_currently_effective(self):
        """True if the rule has no date bounds, or today falls within them."""
        from django.utils import timezone
        today = timezone.now().date()
        if self.effective_from and today < self.effective_from:
            return False
        if self.effective_to and today > self.effective_to:
            return False
        return True


class Citation(models.Model):
    """An additional source passage backing a Rule.

    Optional — a Rule already carries a primary source_ref + source_text.
    Use Citation when a rule rests on more than one source (e.g. a tax
    treatment referencing both a CGI article and a SYSCOHADA chapter).
    """

    rule = models.ForeignKey(
        Rule, on_delete=models.CASCADE, related_name="citations",
    )
    sequence = models.IntegerField(default=0)
    reference = models.CharField(
        max_length=256,
        help_text="e.g. 'SYSCOHADA Titre VIII, Ch. 4, §94'.",
    )
    text = models.TextField(blank=True, help_text="The quoted passage.")
    page = models.IntegerField(null=True, blank=True)
    url = models.URLField(blank=True)

    class Meta:
        ordering = ["rule", "sequence", "id"]

    def __str__(self):
        return self.reference


class TenantProcedure(models.Model):
    """A tenant's own rule, overriding / specialising a framework default.

    Tenant-scoped. Example: "We depreciate vehicles over 4 years, not 5."
    Same structured shape as Rule (trigger_conditions + effects) so the
    proposer agent consumes both uniformly, with tenant procedures taking
    precedence — but the validator (Step 25) blocks any procedure that
    would VIOLATE (not merely specialise) the framework.
    """

    VALIDATION_STATES = [
        ("pending", "Pending validation"),
        ("validated", "Validated — no framework conflict"),
        ("conflict", "Conflicts with framework"),
    ]

    tenant = models.ForeignKey(
        "accounting.Tenant", on_delete=models.CASCADE,
        related_name="procedures",
    )
    slug = models.SlugField(
        max_length=96,
        help_text="Stable id, unique per tenant, e.g. 'depr-vehicles-4y'.",
    )
    title = models.CharField(max_length=256)
    description = models.TextField(
        blank=True,
        help_text="Plain statement of the procedure, e.g. "
                  "'Vehicles depreciate straight-line over 4 years.'",
    )

    trigger_conditions = models.JSONField(default=dict, blank=True)
    effects = models.JSONField(default=dict, blank=True)

    # Which framework rule this procedure overrides / specialises (optional).
    overrides_rule = models.ForeignKey(
        Rule, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="overridden_by",
    )

    validation_status = models.CharField(
        max_length=16, choices=VALIDATION_STATES, default="pending",
    )
    validation_notes = models.TextField(blank=True)

    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    objects = TenantManager()

    class Meta:
        ordering = ["tenant", "slug"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "slug"],
                name="unique_procedure_slug_per_tenant",
            ),
        ]

    def __str__(self):
        return f"{self.tenant} · {self.title}"
