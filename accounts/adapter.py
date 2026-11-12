from allauth.account.adapter import DefaultAccountAdapter


class ClosedSignupAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False

    def send_password_reset_mail(self, user, email, context):
        # A shared demonstration account keeps its published password (accounts.middleware).
        # The page answers as for any address, so it does not reveal which accounts are shared.
        if user.credentials_locked:
            return
        super().send_password_reset_mail(user, email, context)
