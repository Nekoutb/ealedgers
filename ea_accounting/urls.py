"""URL configuration for ea_accounting project."""

from django.contrib import admin
from django.contrib.auth.views import LoginView
from django.urls import include, path

from accounting.views import workspace


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
    # Post-login module launcher
    path('workspace/', workspace, name='workspace'),
    # The Accounting module (custom views — dashboard + reports)
    path('accounting/', include('accounting.urls', namespace='accounting')),
    # Django admin houses the CRUD UIs that the module nav links into
    path('admin/', admin.site.urls),
]
