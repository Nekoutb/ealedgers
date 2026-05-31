from django.apps import AppConfig


class KnowledgeConfig(AppConfig):
    """L3 — Knowledge layer.

    Holds the versioned, sourced, retrievable rule base: accounting
    frameworks (SYSCOHADA, IFRS, …), tax codes (CGI Cameroun 2025, …), and
    per-tenant procedures. The reasoning layer (the agents) queries this to
    decide and to cite. Models land in Step 13 (Rule / Citation /
    TenantProcedure / RuleVector); knowledge encoding begins Step 15.

    Scaffolded empty in Step 11.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'knowledge'
    verbose_name = 'Knowledge (frameworks, tax codes, procedures)'
