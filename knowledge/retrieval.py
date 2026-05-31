"""Rule retrieval (Step 14).

``retrieve(query, ...)`` returns the rules most relevant to a free-text
query (typically a transaction description the agent is reasoning about),
narrowed by structured filters (framework, scope, jurisdiction, slice) and
ranked by relevance.

Ranking strategy is **layered and upgradeable**, signature stable:

  - **Now (SQLite + Postgres):** portable keyword overlap — query terms
    matched against each rule's title (weighted 3), source_ref (2),
    source_text (1), and structured trigger/effects JSON (1).
  - **After Postgres cutover (Step 3):** swap the keyword pass for
    ``SearchVector``/``SearchRank`` full-text ranking.
  - **After pgvector (Step 5):** blend in cosine similarity on the
    RuleVector embedding for true semantic recall.

Callers (the Retriever agent, Step 45+) depend only on ``retrieve()``'s
contract, so these upgrades are internal.

Tenant procedures are ranked alongside framework rules when a ``tenant`` is
supplied, because a tenant's own procedure should surface next to (and
above, when equally relevant) the framework default it specialises.
"""

import re

from knowledge.models import Rule, TenantProcedure


# Minimal bilingual (EN/FR) stopword set — these never help relevance.
_STOPWORDS = {
    # English
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can",
    "with", "from", "this", "that", "was", "has", "had", "have", "will",
    "into", "per", "via", "its", "our", "out", "use", "used",
    # French
    "les", "des", "une", "uns", "dans", "par", "pour", "sur", "avec", "est",
    "son", "ses", "aux", "que", "qui", "pas", "plus", "ont", "été", "leur",
}


def _tokenize(text):
    """Lowercased word tokens of length >= 3, minus stopwords. Keeps
    accented Latin characters (French) and digits (account codes)."""
    if not text:
        return []
    tokens = re.findall(r"[0-9a-zà-ÿ]+", str(text).lower())
    return [t for t in tokens if len(t) >= 3 and t not in _STOPWORDS]


def _flatten_json(value):
    """Yield scalar string fragments from a nested JSON structure so its
    values (e.g. {'asset_class': 'vehicles'}) contribute to matching."""
    if isinstance(value, dict):
        for v in value.values():
            yield from _flatten_json(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _flatten_json(v)
    elif value is not None:
        yield str(value)


def _rule_term_sets(rule):
    """Return (title_terms, ref_terms, body_terms, json_terms) for a Rule."""
    title = set(_tokenize(rule.title))
    ref = set(_tokenize(rule.source_ref))
    body = set(_tokenize(rule.source_text))
    json_text = " ".join(_flatten_json(rule.trigger_conditions))
    json_text += " " + " ".join(_flatten_json(rule.effects))
    return title, ref, body, set(_tokenize(json_text))


def _procedure_term_sets(proc):
    """Same shape as _rule_term_sets for a TenantProcedure (no source_ref;
    description fills the 'body' role)."""
    title = set(_tokenize(proc.title))
    body = set(_tokenize(proc.description))
    json_text = " ".join(_flatten_json(proc.trigger_conditions))
    json_text += " " + " ".join(_flatten_json(proc.effects))
    return title, set(), body, set(_tokenize(json_text))


def _score(term_sets, query_terms):
    """Weighted keyword-overlap score. title=3, ref=2, body=1, json=1."""
    if not query_terms:
        return 0
    title, ref, body, jsn = term_sets
    score = 0
    for term in set(query_terms):
        if term in title:
            score += 3
        if term in ref:
            score += 2
        if term in body:
            score += 1
        if term in jsn:
            score += 1
    return score


def retrieve(
    query,
    *,
    tenant=None,
    framework=None,
    scope=None,
    jurisdiction=None,
    knowledge_slice=None,
    k=10,
    only_active=True,
    only_effective=True,
    include_procedures=True,
):
    """Return up to ``k`` results most relevant to ``query``.

    Each result is a dict:
        {
            "kind": "rule" | "procedure",
            "slug": str,
            "title": str,
            "framework": str | None,   # None for procedures
            "score": int,
            "source_ref": str,
            "object": <Rule|TenantProcedure instance>,
        }

    Results are sorted by score desc, then a procedure outranks a rule at
    equal score (tenant intent wins), then slug for determinism.

    When ``query`` has searchable terms, zero-score candidates are dropped.
    When ``query`` is empty/blank, the filtered set is returned in default
    order (so filters alone still yield results — useful for "show me all
    SYSCOHADA class-2 rules").
    """
    query_terms = _tokenize(query)

    rule_qs = Rule.objects.all()
    if only_active:
        rule_qs = rule_qs.filter(active=True)
    if framework:
        rule_qs = rule_qs.filter(framework=framework)
    if scope:
        rule_qs = rule_qs.filter(scope=scope)
    if jurisdiction:
        rule_qs = rule_qs.filter(jurisdiction=jurisdiction)
    if knowledge_slice:
        rule_qs = rule_qs.filter(knowledge_slice=knowledge_slice)

    results = []
    for rule in rule_qs:
        if only_effective and not rule.is_currently_effective:
            continue
        results.append({
            "kind": "rule",
            "slug": rule.slug,
            "title": rule.title,
            "framework": rule.framework,
            "score": _score(_rule_term_sets(rule), query_terms),
            "source_ref": rule.source_ref,
            "object": rule,
            "_rank_kind": 0,  # rule ranks below procedure at equal score
        })

    if include_procedures and tenant is not None:
        proc_qs = TenantProcedure.objects.for_tenant(tenant)
        if only_active:
            proc_qs = proc_qs.filter(active=True)
        for proc in proc_qs:
            results.append({
                "kind": "procedure",
                "slug": proc.slug,
                "title": proc.title,
                "framework": None,
                "score": _score(_procedure_term_sets(proc), query_terms),
                "source_ref": "",
                "object": proc,
                "_rank_kind": 1,  # procedure ranks above rule at equal score
            })

    if query_terms:
        results = [r for r in results if r["score"] > 0]

    results.sort(key=lambda r: (-r["score"], -r["_rank_kind"], r["slug"]))
    for r in results:
        r.pop("_rank_kind", None)
    return results[:k]
