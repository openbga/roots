from allauth.account.adapter import DefaultAccountAdapter
from django.core.exceptions import ValidationError


class ClosedSignupAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False

    def pre_authenticate(self, request, **credentials):
        try:
            super().pre_authenticate(request, **credentials)
        except ValidationError:
            # Throttling stops before Django authenticates or emits user_login_failed.
            from .signin_log import log_sign_in_failure

            log_sign_in_failure(sender=type(self), credentials=credentials, request=request)
            raise

    def send_password_reset_mail(self, user, email, context):
        # A shared demonstration account keeps its published password (accounts.middleware).
        # The page answers as for any address, so it does not reveal which accounts are shared.
        if user.credentials_locked:
            return
        super().send_password_reset_mail(user, email, context)
