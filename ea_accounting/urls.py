"""URL configuration for ea_accounting project."""

from django.contrib import admin
from django.contrib.auth.views import LoginView
from django.urls import include, path

from accounting.views import signup, switch_tenant_view, workspace


urlpatterns = [
    # Landing / login form
    path(
        '',
        LoginView.as_view(
            template_name='accounting/landing.html',
            redirect_authenticated_user=True,
        ),
        name='landing',
    ),
    # Self-serve sign-up (creates User + Tenant + Membership atomically)
    path('signup/', signup, name='signup'),
    # Tenant switcher (POST-only — flips the active tenant in session)
    path('tenant/switch/<slug:slug>/', switch_tenant_view, name='switch_tenant'),
    # Post-login module launcher
    path('workspace/', workspace, name='workspace'),
    # The Accounting module (custom views — dashboard + reports)
    path('accounting/', include('accounting.urls', namespace='accounting')),
    # Knowledge layer (Step 14: retrieval API; Step 23: rule explorer UI)
    path('knowledge/', include('knowledge.urls', namespace='knowledge')),
    # Django admin houses the CRUD UIs that the module nav links into
    path('admin/', admin.site.urls),
]
