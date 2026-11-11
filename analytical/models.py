"""Analytical work: batches, QC, results and their configuration.

Policy lives in services.py, qc.py and holding.py. Models carry data and database
constraints only. Result observations are never overwritten: each entry or correction is a
new ResultRevision, and approved revisions are immutable.
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower


class QCType(models.TextChoices):
    METHOD_BLANK = "method_blank", "Method blank"
    LCS = "lcs", "Laboratory control sample"
    MATRIX_SPIKE = "matrix_spike", "Matrix spike"
    MATRIX_SPIKE_DUPLICATE = "matrix_spike_duplicate", "Matrix spike duplicate"
    LAB_DUPLICATE = "lab_duplicate", "Laboratory duplicate"
    CALIBRATION_VERIFICATION = "calibration_verification", "Calibration verification"


class QCMeasure(models.TextChoices):
    """The quantity a QC limit applies to."""

    VALUE = "value", "Measured value"
    RECOVERY = "recovery", "Percent recovery"
    RPD = "rpd", "Relative percent difference"


class ResultBasis(models.TextChoices):
    UNKNOWN = "unknown", "Not recorded"
    WET = "wet", "Wet weight"
    DRY = "dry", "Dry weight"
    NA = "na", "Not applicable"


class ResultKind(models.TextChoices):
    DETECTED = "detected", "Detected"
    NON_DETECT = "non_detect", "Non-detect"


DECIMAL_PLACES = 10  # stored scale for measured values; entries needing more are rejected
VALUE_FIELD = {"max_digits": 24, "decimal_places": DECIMAL_PLACES}


# ---------------------------------------------------------------- configuration


class Qualifier(models.Model):
    """Lab-defined result qualifier. Revisions snapshot code and description."""

    code = models.CharField(max_length=10, unique=True)
    description = models.CharField(max_length=200)
    indicates_holding_time = models.BooleanField(
        default=False,
        help_text="Marks a result as qualified for a holding-time exceedance.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class HoldingTimeRule(models.Model):
    """Start event, end event and duration for a method and matrix."""

    class Start(models.TextChoices):
        COLLECTION = "collection", "Collection (composite: end of collection)"
        PREPARATION = "preparation", "Preparation start"

    class End(models.TextChoices):
        PREPARATION = "preparation", "Preparation start"
        ANALYSIS = "analysis", "Analysis start"

    method = models.ForeignKey("catalog.Method", on_delete=models.PROTECT)
    matrix = models.CharField(
        max_length=20, choices=[("water", "Water"), ("wastewater", "Wastewater"), ("soil", "Soil")]
    )
    name = models.CharField(max_length=120)
    start_event = models.CharField(max_length=20, choices=Start.choices)
    end_event = models.CharField(max_length=20, choices=End.choices)
    duration = models.DurationField(help_text="Maximum time from start event to end event.")
    warning_window = models.DurationField(
        null=True,
        blank=True,
        help_text="Flag as at risk when this much time or less remains. Blank: no warning.",
    )
    basis = models.TextField(blank=True, help_text="Method or SOP reference for this limit.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["method__code", "method__version", "matrix", "start_event", "end_event"]
        constraints = [
            models.UniqueConstraint(
                fields=["method", "matrix", "start_event", "end_event"],
                name="analytical_holding_rule_unique",
            ),
            models.CheckConstraint(
                condition=Q(duration__gt=timedelta(0)), name="analytical_holding_duration_pos"
            ),
            models.CheckConstraint(
                condition=Q(start_event="collection")
                | (Q(start_event="preparation") & Q(end_event="analysis")),
                name="analytical_holding_event_order",
            ),
        ]

    def clean(self):
        if self.start_event == "preparation" and self.end_event != "analysis":
            raise ValidationError("A preparation-start clock can only end at analysis start.")
        if self.duration is not None and self.duration <= timedelta(0):
            raise ValidationError({"duration": "Duration must be positive."})

    def __str__(self):
        return f"{self.name} ({self.method})"


class QCRequirement(models.Model):
    """Per-method QC frequency: at least min_per_batch, and one per every_n_samples."""

    method = models.ForeignKey("catalog.Method", on_delete=models.PROTECT)
    qc_type = models.CharField(max_length=40, choices=QCType.choices)
    min_per_batch = models.PositiveSmallIntegerField(default=1)
    every_n_samples = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Also require one per this many field samples (blank: none).",
    )
    basis = models.TextField(blank=True, help_text="Method or SOP reference.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["method__code", "method__version", "qc_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["method", "qc_type"], name="analytical_qc_requirement_unique"
            ),
            models.CheckConstraint(
                condition=Q(every_n_samples__isnull=True) | Q(every_n_samples__gte=1),
                name="analytical_qc_every_n_pos",
            ),
        ]

    def required_count(self, field_sample_count):
        count = self.min_per_batch
        if self.every_n_samples:
            count = max(count, -(-field_sample_count // self.every_n_samples))
        return count

    def __str__(self):
        return f"{self.get_qc_type_display()} · {self.method}"


class QCLimit(models.Model):
    """Acceptance limits for one QC type and measure of one test (method, analyte, matrix)."""

    definition = models.ForeignKey("catalog.TestDefinition", on_delete=models.PROTECT)
    phase = models.CharField(
        max_length=12,
        default="analysis",
        db_default="analysis",
        choices=[("analysis", "Analysis"), ("preparation", "Preparation")],
    )
    qc_type = models.CharField(max_length=40, choices=QCType.choices)
    measure = models.CharField(max_length=20, choices=QCMeasure.choices)
    lower = models.DecimalField(max_digits=24, decimal_places=DECIMAL_PLACES, null=True, blank=True)
    upper = models.DecimalField(max_digits=24, decimal_places=DECIMAL_PLACES, null=True, blank=True)
    basis = models.TextField(blank=True, help_text="Method or SOP reference for these limits.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["definition__method__code", "definition__analyte__name", "qc_type", "measure"]
        constraints = [
            models.UniqueConstraint(
                fields=["definition", "phase", "qc_type", "measure"],
                name="analytical_qc_limit_unique",
            ),
            models.CheckConstraint(
                condition=Q(phase__in=["analysis", "preparation"]), name="qc_limit_phase_valid"
            ),
            models.CheckConstraint(
                condition=Q(lower__isnull=False) | Q(upper__isnull=False),
                name="analytical_qc_limit_has_bound",
            ),
            models.CheckConstraint(
                condition=Q(lower__isnull=True) | Q(upper__isnull=True) | Q(lower__lte=F("upper")),
                name="analytical_qc_limit_order",
            ),
        ]

    def clean(self):
        if self.lower is None and self.upper is None:
            raise ValidationError("Set a lower limit, an upper limit or both.")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValidationError("The lower limit cannot exceed the upper limit.")

    def __str__(self):
        return f"{self.get_qc_type_display()} {self.get_measure_display()} · {self.definition}"


# ---------------------------------------------------------------------- batches


class Batch(models.Model):
    """Analytical batch for one method and one matrix."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    class Kind(models.TextChoices):
        ANALYTICAL = "analytical", "Analytical"
        PREPARATION = "preparation", "Preparation"

    kind = models.CharField(
        max_length=12, choices=Kind.choices, default=Kind.ANALYTICAL, db_default="analytical"
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    method = models.ForeignKey(
        "catalog.Method", on_delete=models.PROTECT, related_name="analytical_batches"
    )
    matrix = models.CharField(
        max_length=20, choices=[("water", "Water"), ("wastewater", "Wastewater"), ("soil", "Soil")]
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    prepared_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Preparation start. Needed where a holding rule starts or ends at preparation.",
    )
    equipment = models.ForeignKey(
        "laboratory.Equipment",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="analytical_batches",
        help_text="Primary instrument or equipment, recorded for information. Nothing in "
        "approval or release depends on it.",
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(status="open") | Q(closed_by__isnull=False, closed_at__isnull=False),
                name="analytical_batch_closed_attributed",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["analytical", "preparation"]), name="batch_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(completed_at__isnull=True)
                | Q(
                    kind="preparation",
                    prepared_at__isnull=False,
                    completed_at__gte=F("prepared_at"),
                ),
                name="preparation_event_order",
            ),
        ]

    @property
    def number(self):
        prefix = "P" if self.kind == self.Kind.PREPARATION else "B"
        return f"{prefix}-{self.pk:06d}"

    def __str__(self):
        return self.number


class QCSample(models.Model):
    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, related_name="qc_samples")
    qc_type = models.CharField(max_length=40, choices=QCType.choices)
    label = models.CharField(max_length=60)
    parent_sample = models.ForeignKey(
        "receipt.Sample",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Field sample spiked or duplicated, where applicable.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["batch_id", "qc_type", "label"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "label"], name="analytical_qc_sample_label"),
        ]

    def __str__(self):
        return f"{self.label} ({self.get_qc_type_display()})"


class QCMeasurement(models.Model):
    """One QC observation for one analyte. Corrections supersede; nothing is overwritten.

    Evaluation is computed from these inputs and the configured limits (see qc.py).
    """

    qc_sample = models.ForeignKey(QCSample, on_delete=models.PROTECT, related_name="measurements")
    analysis_batch = models.ForeignKey(
        Batch,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="preparation_qc_measurements",
        help_text="Actual analytical run for a preparation QC observation; never inferred.",
    )
    definition = models.ForeignKey(
        "catalog.TestDefinition", on_delete=models.PROTECT, related_name="+"
    )
    analyte_name = models.CharField(max_length=160)
    unit = models.CharField(max_length=40)
    observed_text = models.CharField(max_length=60, help_text="Exactly as supplied.")
    kind = models.CharField(max_length=12, choices=ResultKind.choices)
    observed_value = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    reporting_limit_applied = models.DecimalField(**VALUE_FIELD)
    spike_value = models.DecimalField(
        null=True,
        blank=True,
        **VALUE_FIELD,
        help_text="Amount added or true value, in the result unit.",
    )
    native_value = models.DecimalField(
        null=True,
        blank=True,
        **VALUE_FIELD,
        help_text="Unspiked sample result used for recovery. Enter 0 explicitly if not detected.",
    )
    reference_value = models.DecimalField(
        null=True,
        blank=True,
        **VALUE_FIELD,
        help_text="Original result compared for relative percent difference.",
    )
    analysis_started_at = models.DateTimeField()
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    entered_at = models.DateTimeField(auto_now_add=True)
    is_current = models.BooleanField(default=True)
    supersedes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    reason = models.TextField(blank=True, help_text="Required when superseding a measurement.")

    class Meta:
        ordering = ["qc_sample_id", "analyte_name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["qc_sample", "definition"],
                condition=Q(is_current=True),
                name="analytical_qc_measurement_current",
            ),
            models.CheckConstraint(
                condition=(
                    Q(kind="detected", observed_value__isnull=False)
                    | Q(kind="non_detect", observed_value__isnull=True)
                ),
                name="analytical_qc_measurement_kind",
            ),
            models.CheckConstraint(
                condition=Q(reporting_limit_applied__gt=0), name="analytical_qc_measurement_rl_pos"
            ),
        ]


class QCCorrectiveAction(models.Model):
    """Recorded disposition for a QC failure or shortfall. Append-only; the latest per target
    governs, so an acceptance can be superseded by a later reanalysis or rejection."""

    class Disposition(models.TextChoices):
        ACCEPT_QUALIFIED = "accept_qualified", "Accept data with qualification"
        REANALYZE = "reanalyze", "Reanalysis required"
        REJECT_DATA = "reject_data", "Reject affected data"

    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, related_name="corrective_actions")
    target_key = models.CharField(max_length=80)
    qc_type = models.CharField(max_length=40, choices=QCType.choices)
    qc_sample = models.ForeignKey(
        QCSample, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    definition = models.ForeignKey(
        "catalog.TestDefinition", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    measurement = models.ForeignKey(
        QCMeasurement,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="corrective_actions",
    )
    disposition = models.CharField(max_length=20, choices=Disposition.choices)
    note = models.TextField()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        indexes = [models.Index(fields=["batch", "target_key"])]


# ---------------------------------------------------------------------- results


class Result(models.Model):
    """The logical result of one requested test; its observations are ResultRevisions."""

    class Status(models.TextChoices):
        AWAITING = "awaiting", "Awaiting entry"
        DRAFT = "draft", "Entered"
        SUBMITTED = "submitted", "Submitted for review"
        RETURNED = "returned", "Returned"
        APPROVED = "approved", "Approved"

    requested_test = models.OneToOneField(
        "receipt.RequestedTest", on_delete=models.PROTECT, related_name="result"
    )
    batch = models.ForeignKey(
        Batch, null=True, blank=True, on_delete=models.PROTECT, related_name="results"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.AWAITING)
    latest_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status="awaiting", latest_number=0)
                | (~Q(status="awaiting") & Q(latest_number__gte=1)),
                name="analytical_result_status_number",
            ),
        ]

    def __str__(self):
        return f"Result of {self.requested_test_id}"


TRANSITION_FIELDS = frozenset(
    {
        "status",
        "submitted_by",
        "submitted_at",
        "reviewed_by",
        "reviewed_at",
        "review_note",
        "qc_evidence",
    }
)


class ResultRevision(models.Model):
    """One immutable observation of a requested test.

    Only the workflow fields in TRANSITION_FIELDS change after creation, and only while the
    revision is draft or submitted. Approved and returned revisions never change.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Entered"
        SUBMITTED = "submitted", "Submitted for review"
        RETURNED = "returned", "Returned"
        APPROVED = "approved", "Approved"
        SUPERSEDED = "superseded", "Superseded"

    class HoldingStatus(models.TextChoices):
        MET = "met", "Met"
        EXCEEDED = "exceeded", "Exceeded"
        NOT_EVALUABLE = "not_evaluable", "Not evaluable"
        NOT_EVALUATED = "not_evaluated", "No holding rule configured"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual entry"
        IMPORT = "import", "File import"

    result = models.ForeignKey(Result, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, related_name="revisions")
    prepared_aliquot = models.ForeignKey(
        "PreparedAliquot", null=True, blank=True, on_delete=models.PROTECT, related_name="revisions"
    )

    # Provenance snapshots: what was analysed, by which method version, in which unit.
    definition = models.ForeignKey(
        "catalog.TestDefinition", on_delete=models.PROTECT, related_name="+"
    )
    method_code = models.CharField(max_length=60)
    method_version = models.CharField(max_length=40)
    analyte_name = models.CharField(max_length=160)
    matrix = models.CharField(max_length=20)

    # The observation. supplied_text is exactly what the analyst or file supplied.
    supplied_text = models.CharField(max_length=60)
    kind = models.CharField(max_length=12, choices=ResultKind.choices)
    value = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    unit = models.CharField(max_length=40)
    reporting_limit_applied = models.DecimalField(**VALUE_FIELD)
    method_reporting_limit = models.DecimalField(
        null=True,
        blank=True,
        **VALUE_FIELD,
        help_text="Catalog reporting limit when the result was entered, for comparison.",
    )
    result_basis = models.CharField(
        max_length=7, choices=ResultBasis.choices, default=ResultBasis.UNKNOWN
    )
    percent_solids = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    solids_reference = models.CharField(max_length=240, blank=True, default="")
    dilution_factor = models.DecimalField(max_digits=14, decimal_places=4)
    qualifiers = models.JSONField(
        default=list, help_text="Snapshot: [{code, description, holding_time}]."
    )
    analysis_started_at = models.DateTimeField()
    prepared_at = models.DateTimeField(null=True, blank=True)
    comments = models.TextField(blank=True)

    holding_status = models.CharField(max_length=14, choices=HoldingStatus.choices)
    holding_evaluation = models.JSONField(default=list)

    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    source_detail = models.CharField(max_length=200, blank=True)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    entered_at = models.DateTimeField(auto_now_add=True)
    correction_reason = models.TextField(blank=True)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    qc_evidence = models.JSONField(
        default=dict,
        blank=True,
        help_text="Frozen at approval: QC measurements, evaluations and dispositions relied on.",
    )

    @property
    def basis_snapshot(self):
        value = None
        if self.percent_solids is not None:
            value = format(self.percent_solids, "f")
            if "." in value:
                value = value.rstrip("0").rstrip(".")
        return {
            "result_basis": self.result_basis,
            "percent_solids": value,
            "solids_reference": self.solids_reference,
        }

    class Meta:
        ordering = ["result_id", "number"]
        constraints = [
            models.CheckConstraint(
                condition=Q(result_basis__in=ResultBasis.values),
                name="analytical_result_basis_valid",
            ),
            models.CheckConstraint(
                condition=Q(percent_solids__isnull=True)
                | Q(percent_solids__gte=0, percent_solids__lte=100),
                name="analytical_solids_percent_range",
            ),
            models.CheckConstraint(
                condition=Q(percent_solids__isnull=True, solids_reference="")
                | (Q(percent_solids__isnull=False) & ~Q(solids_reference="")),
                name="analytical_solids_reference_pair",
            ),
            models.CheckConstraint(
                condition=Q(matrix="soil")
                | Q(
                    percent_solids__isnull=True,
                    solids_reference="",
                    result_basis__in=["unknown", "na"],
                ),
                name="analytical_soil_metadata_matrix",
            ),
            models.UniqueConstraint(fields=["result", "number"], name="analytical_revision_number"),
            models.CheckConstraint(
                condition=(
                    Q(kind="detected", value__isnull=False)
                    | Q(kind="non_detect", value__isnull=True)
                ),
                name="analytical_revision_kind_value",
            ),
            models.CheckConstraint(
                condition=Q(reporting_limit_applied__gt=0), name="analytical_revision_rl_pos"
            ),
            models.CheckConstraint(
                condition=Q(dilution_factor__gt=0), name="analytical_revision_dilution_pos"
            ),
            models.CheckConstraint(
                condition=Q(number=1) | ~Q(correction_reason=""), name="analytical_revision_reason"
            ),
            models.CheckConstraint(
                condition=Q(status__in=["draft", "superseded"])
                | Q(submitted_by__isnull=False, submitted_at__isnull=False),
                name="analytical_revision_submitted_attributed",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=["approved", "returned"])
                | Q(reviewed_by__isnull=False, reviewed_at__isnull=False),
                name="analytical_revision_reviewed_attributed",
            ),
            models.CheckConstraint(
                condition=Q(status="returned", review_note__gt="") | ~Q(status="returned"),
                name="analytical_revision_return_reason",
            ),
            # No self-review, enforced by the database as well as the service.
            models.CheckConstraint(
                condition=Q(reviewed_by__isnull=True)
                | (~Q(reviewed_by=F("entered_by")) & ~Q(reviewed_by=F("submitted_by"))),
                name="analytical_revision_no_self_review",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = ResultRevision.objects.filter(pk=self.pk).values_list("status", flat=True)
            stored_status = stored.first()
            update_fields = kwargs.get("update_fields")
            if (
                stored_status not in ("draft", "submitted")
                or update_fields is None
                or not set(update_fields) <= TRANSITION_FIELDS
            ):
                raise ValidationError("Result revisions are immutable once reviewed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Result revisions cannot be deleted.")

    @property
    def is_nondetect(self):
        return self.kind == ResultKind.NON_DETECT

    def __str__(self):
        return f"{self.result_id} r{self.number}"


# ------------------------------------------------------------------- MDL studies
# 40 CFR Part 136 Appendix B (Revision 2). A study collects observations; each calculation is an
# immutable numbered snapshot of its inputs and results; an independent reviewer decides it.


class MDLStudy(models.Model):
    """Records for one method detection limit: one test (method, analyte, matrix)."""

    class Kind(models.TextChoices):
        INITIAL = "initial", "Initial determination"
        VERIFICATION = "verification", "Ongoing verification"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    definition = models.ForeignKey(
        "catalog.TestDefinition", on_delete=models.PROTECT, related_name="mdl_studies"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    spiking_level = models.DecimalField(
        **VALUE_FIELD, help_text="Spiking level in the test unit, the same for every spike used."
    )
    instrument_note = models.CharField(
        max_length=200, blank=True, help_text="Instruments this MDL is to apply to, if several."
    )
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(spiking_level__gt=0), name="analytical_mdl_study_level_pos"
            ),
            models.CheckConstraint(
                condition=Q(status="open") | Q(closed_by__isnull=False, closed_at__isnull=False),
                name="analytical_mdl_study_closed_attributed",
            ),
        ]

    @property
    def number(self):
        return f"MDL-{self.pk:05d}"

    def __str__(self):
        return f"{self.number} {self.definition}"


MDL_EXCLUSION_FIELDS = frozenset({"excluded", "exclusion_reason", "excluded_by", "excluded_at"})


class MDLObservation(models.Model):
    """One spiked sample or method blank result. The observation is immutable; a documented
    gross failure can be excluded (with rationale) without changing what was recorded."""

    class Kind(models.TextChoices):
        SPIKE = "spike", "Spiked sample"
        BLANK = "blank", "Method blank"

    study = models.ForeignKey(MDLStudy, on_delete=models.PROTECT, related_name="observations")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    supplied_text = models.CharField(max_length=60, help_text="Exactly as supplied.")
    result_kind = models.CharField(max_length=12, choices=ResultKind.choices)
    value = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    spike_level = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    qualitative_ok = models.BooleanField(
        null=True, blank=True, help_text="Spikes: the method's qualitative identification was met."
    )
    prepared_at = models.DateTimeField()
    prepared_assumed = models.BooleanField(
        default=False,
        help_text="Preparation time was not recorded and was taken to be the analysis time.",
    )
    analyzed_at = models.DateTimeField()
    batch_label = models.CharField(max_length=60)
    preparation_label = models.CharField(
        max_length=60,
        help_text="Identity of the preparation or extract. One extract analysed on several "
        "instruments is one preparation (Appendix B II.2(b)(iii)).",
    )
    instrument = models.CharField(max_length=80, blank=True)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    entered_at = models.DateTimeField(auto_now_add=True)
    source_measurement = models.ForeignKey(
        QCMeasurement,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Routine method blank this observation was taken from.",
    )
    source_context = models.JSONField(default=dict, db_default=dict(), blank=True)
    excluded = models.BooleanField(default=False)
    exclusion_reason = models.TextField(blank=True)
    excluded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    excluded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["study_id", "kind", "analyzed_at", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(result_kind="detected", value__isnull=False)
                    | Q(result_kind="non_detect", value__isnull=True)
                ),
                name="analytical_mdl_obs_kind_value",
            ),
            models.CheckConstraint(
                condition=(
                    Q(kind="spike", spike_level__isnull=False, qualitative_ok__isnull=False)
                    | Q(kind="blank", spike_level__isnull=True, qualitative_ok__isnull=True)
                ),
                name="analytical_mdl_obs_kind_fields",
            ),
            models.CheckConstraint(
                condition=Q(prepared_at__lte=F("analyzed_at")), name="analytical_mdl_obs_dates"
            ),
            models.CheckConstraint(
                condition=~Q(preparation_label=""), name="analytical_mdl_obs_preparation_named"
            ),
            models.CheckConstraint(
                condition=Q(excluded=False)
                | (
                    ~Q(exclusion_reason="")
                    & Q(excluded_by__isnull=False, excluded_at__isnull=False)
                ),
                name="analytical_mdl_obs_exclusion_documented",
            ),
            models.UniqueConstraint(
                fields=["study", "source_measurement"],
                condition=Q(source_measurement__isnull=False),
                name="analytical_mdl_obs_source_once",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None or not set(update_fields) <= MDL_EXCLUSION_FIELDS:
                raise ValidationError("MDL observations cannot be changed; exclude with a reason.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("MDL observations cannot be deleted.")

    def __str__(self):
        return f"{self.study.number} {self.kind} {self.supplied_text}"


MDL_REVIEW_FIELDS = frozenset(
    {"decision", "reviewed_by", "reviewed_at", "review_note", "adopted_mdl"}
)


class MDLCalculation(models.Model):
    """An immutable numbered calculation: inputs snapshot, every intermediate, and the verdict
    ("not evaluated" with itemized reasons when the data do not meet the procedure). Only the
    independent review fields are set later, once."""

    class Status(models.TextChoices):
        CALCULATED = "calculated", "Calculated"
        NOT_EVALUATED = "not_evaluated", "Not evaluated"

    class Decision(models.TextChoices):
        ADOPTED = "adopted", "Adopt the verified MDL"
        RETAINED = "retained", "Keep the existing MDL"
        REJECTED = "rejected", "Reject this calculation"

    class BlankMethod(models.TextChoices):
        NOT_APPLICABLE = "not_applicable", "Does not apply (no numerical blank)"
        HIGHEST = "highest", "Highest method blank"
        PERCENTILE = "percentile", "99th percentile rank"
        MEAN_T = "mean_t", "Mean plus t times standard deviation"

    study = models.ForeignKey(MDLStudy, on_delete=models.PROTECT, related_name="calculations")
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=14, choices=Status.choices)
    reasons = models.JSONField(default=list)
    calculated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    calculated_at = models.DateTimeField(auto_now_add=True)
    as_of = models.DateTimeField()
    blank_window = models.CharField(max_length=20)
    unit = models.CharField(max_length=40)
    inputs = models.JSONField(default=list)

    spike_n = models.PositiveIntegerField(default=0, help_text="Analyses used.")
    blank_n = models.PositiveIntegerField(default=0, help_text="Analyses used.")
    spike_preparations = models.PositiveIntegerField(default=0)
    blank_preparations = models.PositiveIntegerField(default=0)
    spike_level = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    spike_mean = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    spike_sd = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    mean_recovery_pct = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    t_spike = models.DecimalField(null=True, blank=True, max_digits=8, decimal_places=3)
    t_spike_source = models.CharField(max_length=200, blank=True)
    mdl_s = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    blank_mean = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    blank_sd = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    t_blank = models.DecimalField(null=True, blank=True, max_digits=8, decimal_places=3)
    t_blank_source = models.CharField(max_length=200, blank=True)
    mdl_b = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    mdl_b_method = models.CharField(max_length=16, choices=BlankMethod.choices, blank=True)
    mdl_b_note = models.CharField(max_length=300, blank=True)
    verified_mdl = models.DecimalField(null=True, blank=True, **VALUE_FIELD)

    existing_mdl = models.DecimalField(null=True, blank=True, **VALUE_FIELD)
    ratio_to_existing = models.DecimalField(null=True, blank=True, max_digits=14, decimal_places=6)
    blanks_above_existing = models.PositiveIntegerField(null=True, blank=True)
    retention_eligible = models.BooleanField(null=True, blank=True)

    decision = models.CharField(max_length=10, choices=Decision.choices, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    adopted_mdl = models.DecimalField(null=True, blank=True, **VALUE_FIELD)

    class Meta:
        ordering = ["study_id", "number"]
        constraints = [
            models.UniqueConstraint(fields=["study", "number"], name="analytical_mdl_calc_number"),
            models.CheckConstraint(
                condition=Q(status="not_evaluated", verified_mdl__isnull=True)
                | Q(status="calculated", verified_mdl__isnull=False),
                name="analytical_mdl_calc_verdict",
            ),
            models.CheckConstraint(
                condition=Q(decision="") | Q(reviewed_by__isnull=False, reviewed_at__isnull=False),
                name="analytical_mdl_calc_review_attributed",
            ),
            models.CheckConstraint(
                condition=Q(adopted_mdl__isnull=True) | Q(decision__in=["adopted", "retained"]),
                name="analytical_mdl_calc_adopted_decision",
            ),
            models.CheckConstraint(
                condition=Q(reviewed_by__isnull=True) | ~Q(reviewed_by=F("calculated_by")),
                name="analytical_mdl_calc_no_self_review",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            stored = (
                MDLCalculation.objects.filter(pk=self.pk).values_list("decision", flat=True).first()
            )
            if stored or update_fields is None or not set(update_fields) <= MDL_REVIEW_FIELDS:
                raise ValidationError(
                    "MDL calculations are immutable; only one review is recorded."
                )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("MDL calculations cannot be deleted.")

    @property
    def number_label(self):
        return f"{self.study.number} · calculation {self.number}"

    def __str__(self):
        return self.number_label


# ------------------------------------------------------------------------- import profiles


class ImportProfile(models.Model):
    """A saved description of how an operator's tabular result file maps to a batch import.

    It is a column mapping chosen by the operator, not a validated instrument adapter. Each
    import records a snapshot and hash of the mapping it used, so later edits never change what
    an earlier import meant. `mapping` is validated by analytical.import_profiles."""

    class Layout(models.TextChoices):
        LONG = "long", "Long: one result per row"
        WIDE = "wide", "Wide: one sample per row, one column per analyte"

    class Delimiter(models.TextChoices):
        COMMA = "comma", "Comma"
        TAB = "tab", "Tab"

    name = models.CharField(max_length=80)
    layout = models.CharField(max_length=8, choices=Layout.choices)
    delimiter = models.CharField(max_length=8, choices=Delimiter.choices)
    mapping = models.JSONField(default=dict)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="analytical_import_profile_name_ci"),
            models.CheckConstraint(
                condition=Q(layout__in=["long", "wide"]), name="analytical_import_profile_layout"
            ),
            models.CheckConstraint(
                condition=Q(delimiter__in=["comma", "tab"]),
                name="analytical_import_profile_delimiter",
            ),
        ]

    def __str__(self):
        return self.name


class PreservationRule(models.Model):
    """Lab policy for the exact method version, analyte and matrix of a test."""

    class TemperatureMode(models.TextChoices):
        RANGE = "range", "Configured temperature range"
        NOT_REQUIRED = "not_required", "No temperature requirement"
        MANUAL = "manual", "Manual temperature assessment"

    definition = models.OneToOneField("catalog.TestDefinition", on_delete=models.PROTECT)
    temperature_mode = models.CharField(max_length=20, choices=TemperatureMode.choices)
    lower = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    upper = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    unit = models.CharField(max_length=8, default="°C", choices=[("°C", "°C")])
    container_requirements = models.TextField(blank=True)
    chemical_requirements = models.TextField(blank=True)
    other_requirements = models.TextField(blank=True)
    basis = models.TextField(help_text="Applicable method/SOP, program scope and source revision.")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(temperature_mode="range") & (Q(lower__isnull=False) | Q(upper__isnull=False))
                    | Q(
                        temperature_mode__in=["not_required", "manual"],
                        lower__isnull=True,
                        upper__isnull=True,
                    )
                ),
                name="preservation_temperature_mode_bounds",
            ),
            models.CheckConstraint(
                condition=Q(lower__isnull=True) | Q(upper__isnull=True) | Q(lower__lte=F("upper")),
                name="preservation_bounds_order",
            ),
        ]

    def clean(self):
        if self.temperature_mode == "range":
            if self.lower is None and self.upper is None:
                raise ValidationError("Configure at least one temperature bound.")
        elif self.lower is not None or self.upper is not None:
            raise ValidationError("Only a configured temperature range may have numeric bounds.")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValidationError("The lower bound cannot exceed the upper bound.")
        if not self.basis.strip():
            raise ValidationError({"basis": "Record the applicable method/SOP and source."})

    def __str__(self):
        return f"Preservation · {self.definition}"


class PreservationDecision(models.Model):
    """Append-only assessment; withdrawing a requested test never removes its evidence."""

    class Decision(models.TextChoices):
        PROCEED = "proceed", "Proceed"
        QUALIFY = "qualify", "Qualify"
        HOLD = "hold", "Hold"
        REJECT = "reject", "Reject"

    sample = models.ForeignKey("receipt.Sample", on_delete=models.PROTECT)
    definition = models.ForeignKey("catalog.TestDefinition", on_delete=models.PROTECT)
    evidence = models.JSONField()
    fingerprint = models.CharField(max_length=64)
    decision = models.CharField(max_length=10, choices=Decision.choices)
    reason = models.TextField(max_length=4000)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-recorded_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(decision__in=["proceed", "qualify", "hold", "reject"]),
                name="preservation_decision_valid",
            ),
            models.CheckConstraint(condition=~Q(reason=""), name="preservation_reason_required"),
        ]


class PreparationRequirement(models.Model):
    """Explicit method/version required for independent preparation of this exact test."""

    definition = models.OneToOneField("catalog.TestDefinition", on_delete=models.PROTECT)
    method = models.ForeignKey("catalog.Method", on_delete=models.PROTECT)
    basis = models.TextField(help_text="Laboratory method/SOP source and applicability.")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.definition} · preparation {self.method}"


class PreparedAliquot(models.Model):
    """One physical preparation member; reuse and re-preparation remain distinct identities."""

    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, related_name="prepared_aliquots")
    sample = models.ForeignKey(
        "receipt.Sample", on_delete=models.PROTECT, related_name="prepared_aliquots"
    )
    label = models.CharField(max_length=60)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["batch_id", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "label"], name="preparation_aliquot_label"),
            models.CheckConstraint(
                condition=Q(
                    cancelled_at__isnull=True, cancelled_by__isnull=True, cancellation_reason=""
                )
                | (
                    Q(cancelled_at__isnull=False, cancelled_by__isnull=False)
                    & ~Q(cancellation_reason="")
                ),
                name="preparation_cancel_attributed",
            ),
        ]

    def __str__(self):
        return f"{self.pk}: {self.batch.number} / {self.label} ({self.sample.lab_id})"


class PreparedTest(models.Model):
    aliquot = models.ForeignKey(PreparedAliquot, on_delete=models.PROTECT, related_name="tests")
    requested_test = models.ForeignKey(
        "receipt.RequestedTest", on_delete=models.PROTECT, related_name="preparations"
    )

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["aliquot", "requested_test"], name="preparation_test_unique"
            )
        ]
