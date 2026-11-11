"""Physical sample retention; released analytical and report records remain separate."""

from django.conf import settings
from django.db import models


class SampleStorage(models.Model):
    sample = models.OneToOneField(
        "receipt.Sample", on_delete=models.PROTECT, related_name="storage_record"
    )
    retention_until = models.DateField(null=True, blank=True)
    disposal_hold = models.BooleanField(default=False)
    disposed_at = models.DateTimeField(null=True, blank=True)
    disposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    disposal_reason = models.TextField(blank=True)
    disposal_method = models.CharField(max_length=160, blank=True)
    disposal_destination = models.CharField(max_length=160, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        disposed_at__isnull=True,
                        disposed_by__isnull=True,
                        disposal_reason="",
                        disposal_method="",
                        disposal_destination="",
                    )
                    | models.Q(
                        disposed_at__isnull=False,
                        disposed_by__isnull=False,
                        retention_until__isnull=False,
                        disposal_hold=False,
                        disposal_reason__gt="",
                        disposal_method__gt="",
                        disposal_destination__gt="",
                    )
                ),
                name="receipt_disposal_attributed",
            )
        ]
