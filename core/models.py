import uuid

from django.conf import settings
from django.core.validators import URLValidator
from django.db import models

from receipt.vocabulary import US_STATES

MAX_LOGO_BYTES = 512 * 1024


class LabLogoManager(models.Manager):
    """Default queries never load the image bytes; use `with_image()` to serve or render one."""

    def get_queryset(self):
        return super().get_queryset().defer("image")

    def with_image(self):
        return super().get_queryset()


class LabLogo(models.Model):
    """An image the laboratory uploaded as its logo, kept once per distinct file.

    Rows are never changed or deleted (database trigger), so a report snapshot can name the
    logo it printed by content hash. core.lab_profile validates and stores uploads.
    """

    sha256 = models.CharField(max_length=64, unique=True)
    image = models.BinaryField()
    content_type = models.CharField(max_length=20)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    size = models.PositiveIntegerField()
    filename = models.CharField(max_length=255, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    objects = LabLogoManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(size__gt=0, size__lte=MAX_LOGO_BYTES), name="lab_logo_size"
            )
        ]


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
    # Identity printed on new reports (core.lab_profile). New text columns carry db_default so
    # the previous release's inserts stay valid while old and new instances overlap in a deploy.
    address_line1 = models.CharField("Street address", max_length=160, blank=True, db_default="")
    address_line2 = models.CharField(
        "Suite, building or PO box", max_length=160, blank=True, db_default=""
    )
    city = models.CharField(max_length=80, blank=True, db_default="")
    state = models.CharField(max_length=2, blank=True, db_default="", choices=US_STATES)
    postal_code = models.CharField("ZIP code", max_length=12, blank=True, db_default="")
    phone = models.CharField(max_length=40, blank=True, db_default="")
    email = models.EmailField(blank=True, db_default="")
    website = models.CharField(
        max_length=200,
        blank=True,
        db_default="",
        validators=[URLValidator(schemes=["http", "https"])],
    )
    certificate_note = models.TextField(
        "Certificate statement", blank=True, max_length=600, db_default=""
    )
    logo = models.ForeignKey(
        LabLogo, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="single_lab")]

    @property
    def address_lines(self):
        """Address as display lines; empty when no part is recorded."""
        locality = " ".join(
            part
            for part in (
                f"{self.city}," if self.city and self.state else self.city,
                self.state,
                self.postal_code,
            )
            if part
        )
        return [line for line in (self.address_line1, self.address_line2, locality) if line]


class LabCertificate(models.Model):
    """A certification or accreditation the laboratory holds, as the laboratory records it.

    The application keeps no scope register: it never checks a result against a certificate
    and makes no certification or accreditation claim. core.lab_profile holds who may change
    one and when one is printed on a report.
    """

    authority = models.CharField("Issuing authority", max_length=160)
    program = models.CharField("Program or scope", max_length=160, blank=True)
    number = models.CharField("Certificate or laboratory ID", max_length=80)
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    show_on_reports = models.BooleanField("Print on reports", default=False)
    position = models.PositiveSmallIntegerField("List position", default=0)

    class Meta:
        ordering = ["position", "authority", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(valid_from__isnull=True)
                | models.Q(valid_until__isnull=True)
                | models.Q(valid_until__gte=models.F("valid_from")),
                name="lab_certificate_valid_range",
                violation_error_message="Valid until cannot be earlier than Valid from.",
            )
        ]

    def __str__(self):
        return f"{self.authority} {self.number}"


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


class UserPreference(models.Model):
    """One presentation setting of one user (core.preferences): a grid's columns, its page size
    and sort, the sidebar's state. Kept on the server, because lab workstations are shared."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preferences"
    )
    key = models.CharField(max_length=120)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="unique_user_preference")
        ]

    def __str__(self):
        return f"{self.user} {self.key}"
