"""External dispatch and immutable external source observations."""

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from analytical.models import VALUE_FIELD, ResultKind

MAX_SOURCE_PDF_BYTES = 10 * 1024 * 1024


class Subcontract(models.Model):
    requested_test = models.ForeignKey(
        "receipt.RequestedTest", on_delete=models.PROTECT, related_name="subcontracts"
    )
    external_lab = models.CharField(max_length=160)
    external_reference = models.CharField(max_length=160)
    external_sample_id = models.CharField(max_length=160, blank=True)
    dispatched_at = models.DateTimeField()
    from_location = models.CharField(max_length=160)
    handed_to = models.CharField(max_length=160)
    selected_containers = models.JSONField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revision = models.PositiveIntegerField(default=1)
    latest_number = models.PositiveIntegerField(default=0)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["requested_test"],
                condition=Q(cancelled_at__isnull=True),
                name="receipt_one_active_subcontract",
            ),
            models.CheckConstraint(
                condition=Q(
                    cancelled_at__isnull=True, cancelled_by__isnull=True, cancellation_reason=""
                )
                | Q(
                    cancelled_at__isnull=False,
                    cancelled_by__isnull=False,
                    cancellation_reason__gt="",
                    latest_number=0,
                ),
                name="receipt_subcontract_cancellation",
            ),
        ]

    @property
    def number(self):
        return f"EXT-{self.pk:06d}"

    def __str__(self):
        return self.number


class ExternalSourceManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().defer("source_pdf")


class SubcontractResult(models.Model):
    class Decision(models.TextChoices):
        ACCEPTED = "accepted", "Accepted source"
        REJECTED = "rejected", "Rejected source"

    subcontract = models.ForeignKey(Subcontract, on_delete=models.PROTECT, related_name="results")
    number = models.PositiveIntegerField()
    observed_text = models.CharField(max_length=60)
    kind = models.CharField(max_length=12, choices=ResultKind.choices)
    value = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    reporting_limit = models.DecimalField(**VALUE_FIELD)
    dilution = models.DecimalField(**VALUE_FIELD)
    unit = models.CharField(max_length=40)
    external_method = models.CharField(max_length=200)
    external_analyst = models.CharField(max_length=160)
    analyzed_at = models.DateTimeField()
    qualifier_text = models.TextField(blank=True, max_length=4000)
    external_report_reference = models.CharField(max_length=160)
    source_pdf = models.BinaryField()
    source_sha256 = models.CharField(max_length=64)
    source_size = models.PositiveIntegerField()
    source_snapshot = models.JSONField()
    reason = models.TextField(blank=True, max_length=4000)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    entered_at = models.DateTimeField(auto_now_add=True)
    review_decision = models.CharField(max_length=8, choices=Decision.choices, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, max_length=4000)

    objects = ExternalSourceManager()

    class Meta:
        ordering = ["subcontract_id", "-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["subcontract", "number"], name="receipt_subcontract_result_number"
            ),
            models.CheckConstraint(
                condition=Q(kind="detected", value__isnull=False)
                | Q(kind="non_detect", value__isnull=True),
                name="receipt_external_result_kind",
            ),
            models.CheckConstraint(
                condition=Q(reporting_limit__gt=0, dilution__gt=0),
                name="receipt_external_positive_limits",
            ),
            models.CheckConstraint(
                condition=Q(source_size__gt=0, source_size__lte=MAX_SOURCE_PDF_BYTES),
                name="receipt_external_source_size",
            ),
            models.CheckConstraint(
                condition=Q(
                    review_decision="",
                    reviewed_by__isnull=True,
                    reviewed_at__isnull=True,
                    review_note="",
                )
                | Q(
                    review_decision__in=["accepted", "rejected"],
                    reviewed_by__isnull=False,
                    reviewed_at__isnull=False,
                    review_note__gt="",
                ),
                name="receipt_external_review_attributed",
            ),
            models.CheckConstraint(
                condition=Q(reviewed_by__isnull=True) | ~Q(reviewed_by=F("entered_by")),
                name="receipt_external_independent_review",
            ),
            models.CheckConstraint(
                condition=Q(number=1) | Q(reason__gt=""), name="receipt_external_correction_reason"
            ),
        ]

    @property
    def label(self):
        return f"{self.subcontract.number} · source {self.number}"

    @property
    def catalog_unit(self):
        return self.source_snapshot.get("catalog_unit", "")

    @property
    def unit_differs(self):
        """The source reports a unit other than the catalog test's. Both stay as recorded; the
        difference is shown wherever the source is entered, reviewed or read."""
        return bool(self.catalog_unit) and self.unit != self.catalog_unit
