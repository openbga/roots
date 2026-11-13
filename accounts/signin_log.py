"""Operational sign-in events on the console, independent of database resets.

Only stored usernames are logged, never submitted identifiers, passwords or codes.
The console filter attaches the application and Render request IDs.
"""

import logging
from urllib.parse import quote

from allauth.account.signals import login_code_rejected
from allauth.mfa.signals import authentication_failed
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models import Q
from django.dispatch import receiver

logger = logging.getLogger("roots.signin")


def _log(event, user, request):
    username = quote(user.get_username(), safe="@.+-_") if user is not None else "<unknown>"
    logger.info("%s user=%s", event, username, extra={"request": request})


@receiver(user_logged_in, dispatch_uid="roots_signin_success")
def log_sign_in(sender, request, user, **kwargs):
    # Django emits this only after all required authentication stages have completed.
    _log("signin.success", user, request)


@receiver(user_login_failed, dispatch_uid="roots_signin_failure")
def log_sign_in_failure(sender, credentials, request=None, **kwargs):
    matches = Q()
    for field in ("username", "email"):
        identifier = credentials.get(field)
        if isinstance(identifier, str) and identifier:
            matches |= (
                Q(username__iexact=identifier)
                | Q(email__iexact=identifier)
                | Q(emailaddress__email__iexact=identifier)
            )
    users = list(get_user_model().objects.filter(matches).distinct()[:2]) if matches else []
    _log("signin.failed", users[0] if len(users) == 1 else None, request)


@receiver(login_code_rejected, dispatch_uid="roots_signin_code_failure")
def log_login_code_failure(sender, request, user, **kwargs):
    _log("signin.failed", user, request)


@receiver(authentication_failed, dispatch_uid="roots_signin_mfa_failure")
def log_mfa_failure(sender, request, user, reauthentication=False, **kwargs):
    if not reauthentication:
        _log("signin.failed", user, request)


@receiver(user_logged_out, dispatch_uid="roots_signout")
def log_sign_out(sender, request, user, **kwargs):
    if user is not None:
        _log("signout", user, request)
