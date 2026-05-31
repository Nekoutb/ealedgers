"""Tenant-procedure framework validator (Step 25).

A tenant procedure may **specialise** a framework default (a stricter cap, a
shorter useful life, an internal policy within the allowed range) — that is
fine. It may **not violate** it (claim a bigger deduction, a faster
tax-deductible depreciation, or otherwise exceed a framework limit). This
module decides which, deterministically, and returns a human-readable note
that **cites the framework rule** when it blocks one.

Scope of the automated checks (v1, deterministic):

  - **structural** — ``trigger_conditions`` / ``effects`` must be JSON
    objects;
  - **deduction-cap conflicts** — a procedure effect that widens a framework
    ceiling (e.g. head-office fees beyond 2.5 %) is a conflict;
  - **depreciation ceiling** — a useful life so short that the implied annual
    rate exceeds the CGI tax-deductible rate for that asset class.

Each detector looks the framework ``Rule`` up by slug so the citation it
emits is the rule's real ``source_ref`` (and stays in sync if the rule is
re-encoded). Deep, open-ended semantic validation is the proposer agent's
job (Phase P05); this layer catches the concrete, checkable breaches now and
is a registry other detectors slot into.

The contract is stable: ``validate_procedure(procedure) -> (status, notes)``
where status is one of ``TenantProcedure.VALIDATION_STATES``.
"""

from .models import Rule


# effect-key → framework ceiling. ``kind="max"`` means the procedure value
# must not exceed ``limit``; ``rule_slug`` is the encoded rule we cite.
FRAMEWORK_CAPS = {
    "head_office_fees_cap_pct": {
        "rule_slug": "cgi-2025-is-headquarters-and-technical-fees-cap",
        "limit": 2.5, "kind": "max",
        "label": "head-office / technical-fee deduction cap",
        "unit": "% of taxable profit",
    },
    "commissions_cap_pct": {
        "rule_slug": "cgi-2025-is-commissions-cap",
        "limit": 1.0, "kind": "max",
        "label": "commissions deduction cap", "unit": "% of purchases",
    },
    "royalties_cap_pct": {
        "rule_slug": "cgi-2025-is-royalties-cap",
        "limit": 2.5, "kind": "max",
        "label": "royalties deduction cap", "unit": "% of taxable profit",
    },
    "loss_carryforward_years": {
        "rule_slug": "cgi-2025-is-loss-carryforward",
        "limit": 4, "kind": "max",
        "label": "loss carry-forward window", "unit": "years",
    },
    "cash_payment_deductible_threshold_fcfa": {
        "rule_slug": "cgi-2025-is-cash-payment-and-invoice-limits",
        "limit": 100000, "kind": "max",
        "label": "cash-paid deductible-charge threshold", "unit": "FCFA",
    },
}

# Asset-class aliases → the key in cgi-2025-is-depreciation-rates effects.
# Kept small and explicit; extend as procedures reveal more vocabulary.
_ASSET_CLASS_TO_CGI = {
    "vehicles": "automobile_leger_ville", "vehicle": "automobile_leger_ville",
    "vehicule": "automobile_leger_ville", "automobile": "automobile_leger_ville",
    "car": "automobile_leger_ville", "cars": "automobile_leger_ville",
    "computers": "materiel_informatique", "computer": "materiel_informatique",
    "it_equipment": "materiel_informatique", "laptops": "materiel_informatique",
    "materiel_informatique": "materiel_informatique",
    "office_furniture": "mobilier_de_bureau", "furniture": "mobilier_de_bureau",
    "mobilier": "mobilier_de_bureau", "mobilier_de_bureau": "mobilier_de_bureau",
}


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _cite(rule_slug, fallback):
    rule = Rule.objects.filter(slug=rule_slug, active=True).first()
    return rule.source_ref if (rule and rule.source_ref) else fallback


def _check_caps(effects):
    """Procedure effects that would widen a framework ceiling."""
    notes = []
    for key, spec in FRAMEWORK_CAPS.items():
        if key not in effects or not _is_number(effects[key]):
            continue
        val = float(effects[key])
        if spec["kind"] == "max" and val > spec["limit"]:
            ref = _cite(spec["rule_slug"], "framework rule")
            notes.append(
                f"This procedure sets the {spec['label']} to "
                f"{_fmt(val)} {spec['unit']}, but the framework caps it at "
                f"{_fmt(spec['limit'])} {spec['unit']} ({ref}). A procedure "
                f"may be stricter than the framework, not more generous."
            )
    return notes


def _check_depreciation(triggers, effects):
    """A useful life whose implied straight-line rate exceeds the CGI
    tax-deductible ceiling for that asset class."""
    asset = str(triggers.get("asset_class", "")).strip().lower()
    cgi_key = _ASSET_CLASS_TO_CGI.get(asset)
    if not cgi_key:
        return None

    # Implied annual rate from the procedure.
    rate = None
    if _is_number(effects.get("depreciation_rate_pct")):
        rate = float(effects["depreciation_rate_pct"])
    elif _is_number(effects.get("useful_life_years")) and effects["useful_life_years"]:
        rate = 100.0 / float(effects["useful_life_years"])
    elif _is_number(effects.get("useful_life_months")) and effects["useful_life_months"]:
        rate = 1200.0 / float(effects["useful_life_months"])
    if rate is None:
        return None

    rule = Rule.objects.filter(
        slug="cgi-2025-is-depreciation-rates", active=True).first()
    if not rule:
        return None
    ceiling = (rule.effects or {}).get("taux_pct", {}).get(cgi_key)
    if not _is_number(ceiling):
        return None

    if rate > float(ceiling) + 0.01:  # epsilon for float equality
        return (
            f"This procedure depreciates '{asset}' at an implied "
            f"{_fmt(rate)} %/yr, but the tax-deductible ceiling for this "
            f"asset class is {_fmt(ceiling)} % ({rule.source_ref}). The "
            f"excess depreciation would be non-deductible (added back); "
            f"tighten the rate to stay within the framework."
        )
    return None


def _fmt(n):
    """Trim trailing .0 so 25.0 -> '25' but 33.33 stays."""
    f = float(n)
    return str(int(f)) if f == int(f) else f"{f:g}"


def validate_procedure(procedure):
    """Return ``(status, notes)`` for a TenantProcedure without saving.

    status ∈ {"validated", "conflict"} — automated checks never leave a
    procedure ``pending`` (that's the pre-validation state).
    """
    triggers = procedure.trigger_conditions or {}
    effects = procedure.effects or {}

    if not isinstance(triggers, dict) or not isinstance(effects, dict):
        return ("conflict",
                "Malformed procedure: trigger conditions and effects must "
                "each be a JSON object.")

    conflicts = _check_caps(effects)
    dep = _check_depreciation(triggers, effects)
    if dep:
        conflicts.append(dep)

    if conflicts:
        return ("conflict", " ".join(conflicts))

    return ("validated",
            "No framework conflict detected by the automated checks. Deep "
            "semantic validation is performed by the proposer agent "
            "(Phase P05).")


def validate_and_apply(procedure):
    """Validate ``procedure`` and persist the resulting status + notes.
    Returns the procedure."""
    status, notes = validate_procedure(procedure)
    procedure.validation_status = status
    procedure.validation_notes = notes
    procedure.save(update_fields=["validation_status", "validation_notes",
                                  "updated_at"])
    return procedure
