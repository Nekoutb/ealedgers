"""Tenant context middleware.

Resolves the active tenant for each authenticated request and attaches it
to ``request.tenant``. Views and admin pages then filter their data by that
tenant.

Resolution order:

1. **Session**: if the user previously selected a tenant via the switcher,
   we honor that as long as they still have an active membership in it.
2. **First active membership**: if there's no session selection, the user
   lands on the tenant they were most recently added to (deterministic and
   stable across requests; we use ``created_at`` ascending so the same
   tenant always wins for a given user).
3. **None**: unauthenticated users, or authenticated users with no
   memberships at all, get ``request.tenant = None``. Views that need a
   tenant should use the ``@tenant_required`` decorator.

Subdomain-based resolution (``acme.ealedgers.com``) will be added later
when we wire DNS wildcards; the contract here stays the same.
"""

from django.shortcuts import redirect
from django.urls import reverse


SESSION_TENANT_KEY = "tenant_slug"


class TenantContextMiddleware:
    """Sets ``request.tenant`` on every request based on the active user's
    membership. Anonymous requests get ``request.tenant = None``."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.tenant = self._resolve_tenant(request)
        return self.get_response(request)

    # ----- resolution ------------------------------------------------------

    def _resolve_tenant(self, request):
        if not getattr(request, "user", None) or not request.user.is_authenticated:
            return None

        # Lazy import to avoid AppRegistryNotReady at module load time.
        from accounting.models import Membership, Tenant

        # 1. Session-stored selection (must still be a valid membership)
        slug = request.session.get(SESSION_TENANT_KEY)
        if slug:
            membership = (
                Membership.objects.filter(
                    user=request.user, tenant__slug=slug, active=True,
                )
                .select_related("tenant")
                .first()
            )
            if membership:
                return membership.tenant
            # The session pointer is stale — drop it and fall through.
            request.session.pop(SESSION_TENANT_KEY, None)

        # 2. Default to the user's first active membership
        membership = (
            Membership.objects.filter(user=request.user, active=True)
            .select_related("tenant")
            .order_by("created_at", "id")
            .first()
        )
        if membership:
            request.session[SESSION_TENANT_KEY] = membership.tenant.slug
            return membership.tenant

        # 3. No memberships at all
        return None


def tenant_required(view_func):
    """View decorator. Bounces the user to the workspace launcher (a safe
    page that doesn't itself need a tenant) when ``request.tenant`` is
    missing — typically because they're authenticated but not yet attached
    to any tenant (edge case that the sign-up flow will eventually handle)."""

    from functools import wraps

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if getattr(request, "tenant", None) is None:
            return redirect(reverse("workspace"))
        return view_func(request, *args, **kwargs)

    return _wrapped


def switch_tenant(request, tenant_slug):
    """Helper used by the (future) tenant-switcher UI. Sets the session
    pointer if the user has a valid membership in the target tenant.
    Returns the Tenant on success, None on failure."""

    from accounting.models import Membership

    membership = (
        Membership.objects.filter(
            user=request.user, tenant__slug=tenant_slug, active=True,
        )
        .select_related("tenant")
        .first()
    )
    if not membership:
        return None
    request.session[SESSION_TENANT_KEY] = membership.tenant.slug
    return membership.tenant
