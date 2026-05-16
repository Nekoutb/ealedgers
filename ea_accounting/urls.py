"""URL configuration for ea_accounting project."""

from django.contrib import admin
from django.contrib.auth.views import LoginView
from django.urls import path

from accounting.views import workspace


urlpatterns = [
    # Public landing page also serves as the login form. Authenticated users
    # are auto-bounced to LOGIN_REDIRECT_URL (the workspace).
    path(
        '',
        LoginView.as_view(
            template_name='accounting/landing.html',
            redirect_authenticated_user=True,
        ),
        name='landing',
    ),
    # Post-login module launcher (Odoo-style apps grid).
    path('workspace/', workspace, name='workspace'),
    # Django admin (currently houses the Accounting module's CRUD UI).
    path('admin/', admin.site.urls),
]
