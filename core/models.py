import uuid

from django.conf import settings
from django.db import models


class Lab(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    name = models.CharField(max_length=160)
    timezone = models.CharField(max_length=64, default="America/New_York")
    record_retention_statement = models.TextField(
        blank=True,
        max_length=8000,
        help_text="Laboratory-entered electronic record-retention policy; no automatic deletion.",
    )
    record_retention_revision = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="single_lab")]


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    object_type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=64)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["object_type", "object_id", "occurred_at"])]


class Announcement(models.Model):
    """A notice the laboratory posts on the sign-in page, for its staff or for client users.

    core.announcements holds who may post one, who sees it and when it counts as showing.
    """

    class Tone(models.TextChoices):
        INFO = "info", "Notice"
        WARNING = "warning", "Important"

    title = models.CharField(max_length=120)
    body = models.TextField(max_length=2000)
    tone = models.CharField(max_length=10, choices=Tone.choices, default=Tone.INFO)
    show_on_sign_in = models.BooleanField(default=False)
    show_to_staff = models.BooleanField(default=False)
    show_to_clients = models.BooleanField(default=False)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-starts_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(show_on_sign_in=True)
                | models.Q(show_to_staff=True)
                | models.Q(show_to_clients=True),
                name="announcement_has_audience",
                violation_error_message="Choose at least one place to show this announcement.",
            ),
            models.CheckConstraint(
                condition=models.Q(ends_at__isnull=True)
                | models.Q(ends_at__gt=models.F("starts_at")),
                name="announcement_ends_after_start",
                violation_error_message="Show until must be later than Show from.",
            ),
        ]

    def __str__(self):
        return self.title
