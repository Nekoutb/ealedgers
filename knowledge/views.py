"""Knowledge-layer views.

  - ``retrieve_view`` (Step 14) — JSON retrieval endpoint the agents call.
  - ``explorer_view`` / ``rule_detail_view`` (Step 23) — the human-facing
    rule explorer: browse / search / filter the global rule base and read a
    rule with its verbatim source and structured DSL, plus copy-citation
    ("cite-from") affordances.

All views are ``@login_required``. The rule base is GLOBAL product data, so
the explorer does not require a tenant; tenant procedures are folded in when
the caller has an active tenant (``request.tenant``).
"""

import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from accounting.middleware import tenant_required

from .forms import TenantProcedureForm, unique_procedure_slug
from .models import Rule, TenantProcedure
from .retrieval import retrieve
from .validation import validate_and_apply


@login_required
@tenant_required
def retrieve_view(request):
    """GET /knowledge/retrieve/?q=...&framework=...&scope=...&k=...

    Returns the top-K rules + tenant procedures relevant to the query,
    scoped to the caller's tenant for procedures. Framework rules are
    global. Read-only; safe for any authenticated tenant user.
    """
    query = request.GET.get("q", "")
    framework = request.GET.get("framework") or None
    scope = request.GET.get("scope") or None
    jurisdiction = request.GET.get("jurisdiction") or None
    knowledge_slice = request.GET.get("slice") or None
    try:
        k = min(max(int(request.GET.get("k", "10")), 1), 50)
    except (TypeError, ValueError):
        k = 10

    results = retrieve(
        query,
        tenant=request.tenant,
        framework=framework,
        scope=scope,
        jurisdiction=jurisdiction,
        knowledge_slice=knowledge_slice,
        k=k,
    )

    # Strip the model instance for JSON; keep the serialisable summary.
    payload = [
        {
            "kind": r["kind"],
            "slug": r["slug"],
            "title": r["title"],
            "framework": r["framework"],
            "score": r["score"],
            "source_ref": r["source_ref"],
        }
        for r in results
    ]
    return JsonResponse({
        "query": query,
        "count": len(payload),
        "results": payload,
    })


# ---------------------------------------------------------------------------
# Step 23 — rule-explorer UI
# ---------------------------------------------------------------------------

# Cap the explorer result set. With ~100 rules today this shows everything;
# it bounds the page if the base grows large before pagination lands.
_EXPLORER_MAX = 200


@login_required
def explorer_view(request):
    """GET /knowledge/explorer/ — browse / search / filter the rule base.

    Free-text ``q`` is ranked by the same ``retrieve()`` the agents use;
    ``framework`` / ``scope`` / ``slice`` narrow the set. With no query and
    no filters, the whole base is listed in its default order.
    """
    query = (request.GET.get("q") or "").strip()
    framework = request.GET.get("framework") or None
    scope = request.GET.get("scope") or None
    knowledge_slice = request.GET.get("slice") or None

    results = retrieve(
        query,
        tenant=getattr(request, "tenant", None),
        framework=framework,
        scope=scope,
        knowledge_slice=knowledge_slice,
        k=_EXPLORER_MAX,
        # The explorer shows the whole catalogue, including rules whose
        # effective window is past/future, so reviewers see everything.
        only_effective=False,
    )

    searching = bool(query)
    rows = [
        {
            "kind": r["kind"],
            "slug": r["slug"],
            "title": r["title"],
            "framework": r["framework"],
            "source_ref": r["source_ref"],
            "score": r["score"],
            "knowledge_slice": getattr(r["object"], "knowledge_slice", ""),
            "scope": getattr(r["object"], "scope", ""),
            "jurisdiction": getattr(r["object"], "jurisdiction", ""),
            "review_status": getattr(r["object"], "review_status", ""),
            "snippet": (getattr(r["object"], "source_text", "")
                        or getattr(r["object"], "description", ""))[:240],
        }
        for r in results
    ]

    # Filter dropdown options + headline stats, computed from the full base.
    all_rules = Rule.objects.all()
    frameworks = sorted(f for f in all_rules.values_list(
        "framework", flat=True).distinct() if f)
    slices = sorted(s for s in all_rules.values_list(
        "knowledge_slice", flat=True).distinct() if s)

    context = {
        "page_name": "Knowledge Base",
        "query": query,
        "rows": rows,
        "searching": searching,
        "result_count": len(rows),
        "total_rules": all_rules.count(),
        "frameworks": frameworks,
        "slices": slices,
        "scopes": [("framework", "Accounting framework"),
                   ("tax_code", "Tax code")],
        "sel_framework": framework or "",
        "sel_scope": scope or "",
        "sel_slice": knowledge_slice or "",
        "pending_review": all_rules.filter(
            review_status="needs_review").count(),
        "has_tenant": getattr(request, "tenant", None) is not None,
    }
    return render(request, "knowledge/explorer.html", context)


@login_required
def rule_detail_view(request, slug):
    """GET /knowledge/explorer/<slug>/ — one rule with its verbatim source,
    structured trigger/effects DSL, citations, and copy-citation actions."""
    rule = get_object_or_404(Rule, slug=slug)
    citation = (
        f'{rule.title} — {rule.source_ref}: "{rule.source_text}"'
        if rule.source_text else f"{rule.title} — {rule.source_ref}"
    )
    context = {
        "page_name": "Knowledge Base",
        "rule": rule,
        "trigger_json": json.dumps(
            rule.trigger_conditions, indent=2, ensure_ascii=False),
        "effects_json": json.dumps(
            rule.effects, indent=2, ensure_ascii=False),
        "citations": rule.citations.all(),
        "citation_text": citation,
    }
    return render(request, "knowledge/rule_detail.html", context)


# ---------------------------------------------------------------------------
# Step 24 — tenant-procedure UI (a tenant's own rules)
# ---------------------------------------------------------------------------


@login_required
@tenant_required
def procedure_list_view(request):
    """GET /knowledge/procedures/ — the active tenant's own procedures."""
    procs = list(
        TenantProcedure.objects.for_tenant(request.tenant)
        .select_related("overrides_rule")
        .order_by("-updated_at")
    )
    return render(request, "knowledge/procedures_list.html", {
        "page_name": "Knowledge Base",
        "procedures": procs,
        "proc_count": len(procs),
    })


@login_required
@tenant_required
def procedure_create_view(request):
    """GET/POST /knowledge/procedures/new/ — create a procedure for the
    active tenant. Saved in ``pending`` state for the Step-25 validator."""
    if request.method == "POST":
        form = TenantProcedureForm(request.POST, tenant=request.tenant)
        if form.is_valid():
            proc = form.save(commit=False)
            proc.tenant = request.tenant
            proc.slug = unique_procedure_slug(
                request.tenant, form.cleaned_data["title"])
            proc.created_by = request.user
            proc.save()
            # Step 25: check it against the framework (specialise vs violate).
            validate_and_apply(proc)
            return redirect(reverse("knowledge:procedure_list"))
    else:
        form = TenantProcedureForm(tenant=request.tenant)
    return render(request, "knowledge/procedure_form.html", {
        "page_name": "Knowledge Base", "form": form, "mode": "create",
    })


@login_required
@tenant_required
def procedure_edit_view(request, slug):
    """GET/POST /knowledge/procedures/<slug>/edit/ — edit one of the active
    tenant's procedures. Editing content resets it to ``pending`` so the
    framework-conflict check re-runs. 404 for another tenant's slug."""
    proc = get_object_or_404(
        TenantProcedure.objects.for_tenant(request.tenant), slug=slug)
    if request.method == "POST":
        form = TenantProcedureForm(
            request.POST, instance=proc, tenant=request.tenant)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.tenant = request.tenant  # ownership never changes
            obj.save()
            # Step 25: edited content is re-checked against the framework.
            validate_and_apply(obj)
            return redirect(reverse("knowledge:procedure_list"))
    else:
        form = TenantProcedureForm(instance=proc, tenant=request.tenant)
    return render(request, "knowledge/procedure_form.html", {
        "page_name": "Knowledge Base", "form": form,
        "mode": "edit", "procedure": proc,
    })
