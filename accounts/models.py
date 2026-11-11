from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    email = models.EmailField(unique=True)
    client = models.ForeignKey(
        "receipt.Client", null=True, blank=True, on_delete=models.PROTECT, related_name="users"
    )

    class Meta(AbstractUser.Meta):
        permissions = [("authorize_release", "Can authorize report release")]
