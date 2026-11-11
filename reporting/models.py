"""Reports. A Report is the editable scope (project, samples, narrative). Each release creates
an immutable ReportVersion; its frozen snapshot and stored PDF live in ReportFile so list
queries never load them. Policy lives in content.py and services.py.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q

MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_SAMPLES = 100
MAX_RESULTS = 1000
MAX_NARRATIVE = 4000


class Report(models.Model):
    project = models.ForeignKey("receipt.Project", on_delete=models.PROTECT, related_name="reports")
    title = models.CharField(max_length=160)
    narrative = models.TextField(
        blank=True, max_length=MAX_NARRATIVE, help_text="Case narrative printed on the report."
    )
    samples = models.ManyToManyField("receipt.Sample", related_name="reports")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revision = models.PositiveIntegerField(default=1, help_text="Draft edit counter.")
    latest_version = models.PositiveIntegerField(
        default=0, help_text="Number of the latest issued version; 0 while never released."
    )

    class Meta:
        ordering = ["-created_at", "-pk"]

    @property
    def number(self):
        return f"RPT-{self.pk:06d}"

    def __str__(self):
        return self.number


class ReportVersion(models.Model):
    """One issued version. Never updated or deleted; an amendment is a new version."""

    report = models.ForeignKey(Report, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    amends = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="amended_by"
    )
    amendment_reason = models.TextField(blank=True)
    changes = models.JSONField(default=list, help_text="What changed from the amended version.")
    content_sha256 = models.CharField(max_length=64)
    pdf_sha256 = models.CharField(max_length=64)
    pdf_size = models.PositiveIntegerField()
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    released_at = models.DateTimeField()
    result_revisions = models.ManyToManyField(
        "analytical.ResultRevision", related_name="report_versions"
    )
    external_result_revisions = models.ManyToManyField(
        "receipt.SubcontractResult",
        related_name="report_versions",
        blank=True,
        help_text="Accepted external source results frozen into this version.",
    )

    class Meta:
        ordering = ["report_id", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["report", "number"], name="reporting_version_number"),
            models.CheckConstraint(
                condition=Q(number=1, amends__isnull=True, amendment_reason="")
                | Q(number__gt=1, amends__isnull=False, amendment_reason__gt=""),
                name="reporting_version_amendment",
            ),
            models.CheckConstraint(
                condition=Q(pdf_size__gt=0, pdf_size__lte=MAX_PDF_BYTES),
                name="reporting_version_pdf_size",
            ),
        ]

    @property
    def label(self):
        return f"{self.report.number} v{self.number}"

    @property
    def filename(self):
        return f"{self.report.number}-v{self.number}.pdf"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Issued report versions cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Issued report versions cannot be deleted.")

    def __str__(self):
        return f"{self.report_id} v{self.number}"


class ReportFile(models.Model):
    """Frozen snapshot and PDF bytes of one version. Downloads return these bytes."""

    version = models.OneToOneField(
        ReportVersion, primary_key=True, on_delete=models.PROTECT, related_name="file"
    )
    snapshot = models.JSONField(encoder=DjangoJSONEncoder)
    pdf = models.BinaryField()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Issued report files cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Issued report files cannot be deleted.")


# ------------------------------------------------------------------ recipient EDD exports


class NysdecProjectSettings(models.Model):
    """NYSDEC EDD codes for one project. The lab enters registered NYSDEC values; none derived."""

    MATRIX_FIELDS = {
        "Water": "water_matrix_code",
        "Wastewater": "wastewater_matrix_code",
        "Soil": "soil_matrix_code",
    }

    project = models.OneToOneField(
        "receipt.Project", on_delete=models.PROTECT, related_name="nysdec_settings"
    )
    data_provider = models.CharField(max_length=20, blank=True, help_text="rt_company code.")
    sampling_company_code = models.CharField(
        max_length=40, blank=True, help_text="rt_company code of the company that sampled."
    )
    lab_name_code = models.CharField(
        max_length=20, blank=True, help_text="rt_company code of this laboratory."
    )
    task_code = models.CharField(max_length=40, blank=True)
    water_matrix_code = models.CharField(max_length=10, blank=True)
    wastewater_matrix_code = models.CharField(max_length=10, blank=True)
    soil_matrix_code = models.CharField(max_length=10, blank=True)
    water_qc_matrix_code = models.CharField(
        max_length=10, blank=True, help_text="rt_matrix LAB-class code for water lab QC samples."
    )
    wastewater_qc_matrix_code = models.CharField(max_length=10, blank=True)
    soil_qc_matrix_code = models.CharField(max_length=10, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)


class NysdecTestMapping(models.Model):
    """NYSDEC codes for one test definition (method version, analyte, matrix)."""

    BASIS = [("WET", "Wet weight"), ("DRY", "Dry weight"), ("NA", "Not applicable")]

    definition = models.OneToOneField(
        "catalog.TestDefinition", on_delete=models.PROTECT, related_name="nysdec_mapping"
    )
    lab_anl_method_name = models.CharField("NYSDEC analytical method", max_length=20, blank=True)
    preparation_method_code = models.CharField(
        "Lab preparation method code", max_length=60, blank=True, db_default=""
    )
    preparation_method_version = models.CharField(
        "Lab preparation method version", max_length=40, blank=True, db_default=""
    )
    prep_method = models.CharField(
        "NYSDEC preparation method", max_length=20, blank=True, db_default=""
    )
    fraction = models.CharField(max_length=10, blank=True)
    preservative = models.CharField(max_length=20, blank=True)
    basis = models.CharField(max_length=3, choices=BASIS, blank=True)
    chemical_name = models.CharField("NYSDEC chemical name", max_length=75, blank=True)
    unit_code = models.CharField("NYSDEC unit", max_length=15, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)


class NysdecQualifierMapping(models.Model):
    """NYSDEC interpreted qualifier (rt_qualifier) for one laboratory qualifier code."""

    qualifier = models.OneToOneField(
        "analytical.Qualifier", on_delete=models.PROTECT, related_name="nysdec_mapping"
    )
    interpreted_qualifier = models.CharField(max_length=10)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)


class EddVersionContext(models.Model):
    """Export-time facts for one released version that its snapshot does not hold: per-sample
    depth and NYSDEC location code, and the NYSDEC test type of a corrected result. Entered by the
    laboratory, audited, frozen into each export; never written back into the release snapshot."""

    version = models.OneToOneField(
        ReportVersion, on_delete=models.PROTECT, related_name="edd_context"
    )
    samples = models.JSONField(
        default=dict, help_text="lab ID -> start_depth, end_depth, depth_unit, sys_loc_code."
    )
    results = models.JSONField(default=dict, help_text="revision id -> test_type.")
    qc_samples = models.JSONField(
        default=dict, help_text="lab QC sample code -> sample_date (ISO), dilution_factor."
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)


class EddExportManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().defer("data")  # lists never load file bytes


class EddExport(models.Model):
    """One generated recipient file for a released version, with the exact mapping values used.
    Never updated or deleted; mapping changes produce a new export and the old one is kept."""

    version = models.ForeignKey(ReportVersion, on_delete=models.PROTECT, related_name="edd_exports")
    format_name = models.CharField(max_length=20)
    format_version = models.CharField(max_length=20)
    reference_values = models.CharField(max_length=20)
    mapping = models.JSONField(help_text="Recipient codes used, frozen at generation.")
    manifest = models.JSONField(
        default=dict, help_text="What was included, omitted and interpreted, frozen at generation."
    )
    row_counts = models.JSONField()
    data = models.BinaryField()
    sha256 = models.CharField(max_length=64)
    size = models.PositiveIntegerField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = EddExportManager()

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(size__gt=0, size__lte=MAX_PDF_BYTES), name="reporting_edd_size"
            ),
        ]

    @property
    def filename(self):
        return (
            f"{self.version.report.number}-v{self.version.number}.{self.format_name}_v5."
            f"EXPLORATORY-{self.pk}.zip"
        )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Generated exports cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Generated exports cannot be deleted.")
