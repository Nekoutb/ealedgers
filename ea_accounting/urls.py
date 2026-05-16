"""URL configuration for ea_accounting project."""

from django.contrib import admin
from django.contrib.auth.views import LoginView
from django.urls import path


urlpatterns = [
    # Public landing page also serves as the login form. Authenticated users
    # are auto-bounced to /admin/. After successful login, redirect to LOGIN_REDIRECT_URL.
    path(
        '',
        LoginView.as_view(
            template_name='accounting/landing.html',
            redirect_authenticated_user=True,
        ),
        name='landing',
    ),
    path('admin/', admin.site.urls),
]
