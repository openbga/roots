"""Shared demonstration accounts keep the credentials their deployment published.

`seed_demo --announce-accounts` publishes one password for several accounts and marks them
`credentials_locked`. Anyone can sign in to them, so nobody may change the password, the email
addresses or the two-factor settings and lock the next visitor out. For such an account every
account-settings page is refused except signing in and out; the user menu hides the links too.
"""

from django.contrib import messages
from django.shortcuts import redirect

ALLOWED_ROUTES = frozenset({"account_login", "account_logout"})
REFUSAL = (
    "This is a shared demonstration account. Its password, email addresses and two-factor "
    "settings cannot be changed."
)


class LockedCredentialsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = request.resolver_match
        settings_page = match.route.startswith("accounts/") or match.view_name.startswith(
            "admin:password_change"
        )
        if not settings_page or match.view_name in ALLOWED_ROUTES:
            return None  # decided from the route alone, without loading the session
        user = request.user
        if not user.is_authenticated or not user.credentials_locked:
            return None
        messages.warning(request, REFUSAL)
        return redirect("dashboard")
