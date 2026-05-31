"""Knowledge-layer views (Step 14).

For now: a JSON retrieval endpoint used to exercise the retriever and,
later, as an internal API the agents call. The human-facing rule-explorer
UI lands in Step 23.
"""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from accounting.middleware import tenant_required

from .retrieval import retrieve


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
