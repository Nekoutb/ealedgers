"""URL configuration for ea_accounting project."""

from django.contrib import admin
from django.contrib.auth.views import LoginView
from django.urls import include, path

from accounting.views import (
    agent_activity,
    approver_assignment,
    capability_matrix,
    departments,
    erp_connection_create,
    erp_connection_edit,
    erp_connections,
    erp_operation_audit,
    signup,
    switch_tenant_view,
    workspace,
)


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
    # Post-login home launcher
    path('workspace/', workspace, name='workspace'),
    # Per-tenant approver assignment (Step 49)
    path('workspace/approvers/', approver_assignment, name='approver_assignment'),
    # Platform pages — the virtual-finance-function vision (read-only skeletons
    # today; the agents fill them from Phase P03/P05+). The accounting data
    # layer itself is reached via the Django admin ("Ledger").
    path('departments/', departments, name='departments'),
    path('agents/', agent_activity, name='agent_activity'),
    path('erp/', erp_connections, name='erp_connections'),
    path('erp/capabilities/', capability_matrix, name='erp_capabilities'),
    path('erp/audit/', erp_operation_audit, name='erp_operation_audit'),
    path('erp/new/', erp_connection_create, name='erp_connection_create'),
    path('erp/<int:pk>/edit/', erp_connection_edit, name='erp_connection_edit'),
    # Knowledge layer (retrieval API + rule explorer + tenant procedures)
    path('knowledge/', include('knowledge.urls', namespace='knowledge')),
    # Django admin houses the CRUD UIs that the module nav links into
    path('admin/', admin.site.urls),
]
