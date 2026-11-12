from django.conf import settings
from django.contrib import admin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, resolve_url
from django.urls import include, path
from django.views.decorators.cache import never_cache

from core import announcement_views, views


@never_cache
def admin_login(request, extra_context=None):
    """Send admin sign-in to the application's sign-in page.

    Django's own admin login accepts a password alone, so it would skip the sign-in rate limits
    and an enrolled second factor. It is never called: anonymous visitors go to the normal
    sign-in, and a signed-in account either may use the admin or is refused.
    """
    if not request.user.is_authenticated:
        target = request.GET.get("next") or resolve_url("admin:index")
        return redirect_to_login(target, resolve_url(settings.LOGIN_URL))
    if request.user.is_active and request.user.is_staff:
        return redirect("admin:index")
    raise PermissionDenied


admin.site.login = admin_login

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("health/", views.health, name="health"),
    path("lab/record-retention/", views.record_retention, name="record-retention"),
    path("lab/announcements/", announcement_views.announcement_list, name="announcement-list"),
    path(
        "lab/announcements/new/",
        announcement_views.announcement_edit,
        name="announcement-create",
    ),
    path(
        "lab/announcements/<int:pk>/",
        announcement_views.announcement_edit,
        name="announcement-edit",
    ),
    path(
        "lab/announcements/<int:pk>/withdraw/",
        announcement_views.announcement_withdraw,
        name="announcement-withdraw",
    ),
    path("ready/", views.ready, name="ready"),
    path("accounts/", include("allauth.urls")),
    path("admin/", admin.site.urls),
    path("", include("receipt.urls")),
    path("catalog/", include("catalog.urls")),
    path("commercial/", include("commercial.urls")),
    path("analytical/", include("analytical.urls")),
    path("laboratory/", include("laboratory.urls")),
    path("reports/", include("reporting.urls")),
    path("portal/", include("reporting.portal_urls")),
    path("portal/", include("commercial.portal_urls")),
]
