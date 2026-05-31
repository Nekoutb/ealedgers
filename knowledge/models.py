"""Knowledge-layer models.

Intentionally empty at Step 11 (scaffold only). The real models arrive in
Step 13:

  - Rule              — one structured rule (trigger conditions + effects +
                        source citation + effective dates + scope)
  - Citation          — link from a Rule to its source passage
  - TenantProcedure   — a tenant's own rule, overriding framework defaults
  - RuleVector        — pgvector embedding of a rule's source text (Step 5
                        installs the pgvector extension first)

See docs/EXECUTION_PLAN.md §3 (Knowledge encoding catalogue, K01–K20).
"""

# (no models yet — Step 13)
