from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    email = models.EmailField(unique=True)
    client = models.ForeignKey(
        "receipt.Client", null=True, blank=True, on_delete=models.PROTECT, related_name="users"
    )
    # Set only by `seed_demo --announce-accounts`, for accounts whose password it publishes.
    # db_default keeps the previous release's inserts valid while both releases run.
    credentials_locked = models.BooleanField(
        default=False,
        db_default=False,
        help_text="Shared demonstration account: whoever signs in cannot change its password, "
        "email addresses or two-factor settings.",
    )

    class Meta(AbstractUser.Meta):
        permissions = [("authorize_release", "Can authorize report release")]
