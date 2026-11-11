"""Laboratory operations records: inventory, equipment, training and nonconformance.

Events (stock movements, equipment events, training records, nonconformance timeline) are
append-only: a mistake is fixed by a linked correction that carries its own reason, never by
editing the earlier row. Current state on the parent rows (lot balance, nonconformance status)
changes only through the service functions, each in one transaction with an audit event.
Evidence files are small, bounded and stored in a separate deferred column (see `evidence`).
Dates that a lab decides (expiry, next due) are stored as entered; nothing here supplies a
default interval.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower

from receipt.models import Sample

MATRICES = Sample.Matrix.choices


class EvidenceManager(models.Manager):
    """Default queries never load attachment bytes; use `with_evidence()` to download."""

    def get_queryset(self):
        return super().get_queryset().defer("evidence_data")

    def with_evidence(self):
        return super().get_queryset()


class EvidenceMixin(models.Model):
    """Optional attached file (bounded, checksummed); a text `reference` stays separate."""

    evidence_name = models.CharField(max_length=255, blank=True, editable=False)
    evidence_content_type = models.CharField(max_length=100, blank=True, editable=False)
    evidence_size = models.PositiveIntegerField(null=True, blank=True, editable=False)
    evidence_sha256 = models.CharField(max_length=64, blank=True, editable=False)
    evidence_data = models.BinaryField(null=True, blank=True, editable=False)

    objects = EvidenceManager()

    class Meta:
        abstract = True

    @property
    def has_evidence(self):
        return bool(self.evidence_name)


def _actor(related_name="+"):
    return models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name=related_name
    )


# ------------------------------------------------------------------ inventory


class Material(models.Model):
    """A kind of standard, reagent or material. Lots of it carry vendor, dates and quantities."""

    class Kind(models.TextChoices):
        STANDARD = "standard", "Standard"
        REAGENT = "reagent", "Reagent"
        MATERIAL = "material", "Other material"

    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    cas_number = models.CharField("CAS number", max_length=30, blank=True)
    specification = models.TextField(
        blank=True, help_text="Grade, purity or certified concentration as specified for purchase."
    )
    unit = models.CharField(
        max_length=40, help_text="Unit all lots of this material are counted in, e.g. mL, g, each."
    )
    storage_requirements = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)
    created_by = _actor()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="lab_material_name_ci"),
        ]

    def __str__(self):
        return self.name


class MaterialLot(models.Model):
    class Source(models.TextChoices):
        PURCHASED = "purchased", "Purchased / supplied"
        PREPARED = "prepared", "Prepared in the lab"

    class Status(models.TextChoices):
        ACTIVE = "active", "In stock"
        RETIRED = "retired", "Retired"

    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="lots")
    lot_number = models.CharField(max_length=80)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.PURCHASED)
    vendor = models.CharField(max_length=160, blank=True)
    vendor_reference = models.CharField(
        max_length=160,
        blank=True,
        help_text="Catalog number, certificate of analysis or traceability reference.",
    )
    stated_concentration = models.CharField(
        max_length=120,
        blank=True,
        help_text="As printed on the certificate or label, with its unit. Not converted.",
    )
    prepared_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="preparations"
    )
    received_on = models.DateField(null=True, blank=True)
    prepared_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(help_text="From the label or set by the lab; never calculated.")
    storage_location = models.CharField(max_length=160)
    storage_conditions = models.CharField(max_length=200, blank=True)
    unit = models.CharField(max_length=40, editable=False)
    quantity_initial = models.DecimalField(max_digits=18, decimal_places=6, editable=False)
    quantity_on_hand = models.DecimalField(
        max_digits=18, decimal_places=6, default=0, editable=False
    )
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE)
    retired_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)
    created_by = _actor()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["expires_on", "material__name", "lot_number"]
        constraints = [
            models.UniqueConstraint(fields=["material", "lot_number"], name="lab_lot_number"),
            models.CheckConstraint(
                condition=Q(quantity_on_hand__gte=0), name="lab_lot_balance_not_negative"
            ),
            models.CheckConstraint(
                condition=Q(received_on__isnull=False) | Q(prepared_on__isnull=False),
                name="lab_lot_has_date",
            ),
            models.CheckConstraint(
                condition=Q(received_on__isnull=True) | Q(expires_on__gte=F("received_on")),
                name="lab_lot_expiry_after_receipt",
            ),
            models.CheckConstraint(
                condition=Q(prepared_on__isnull=True) | Q(expires_on__gte=F("prepared_on")),
                name="lab_lot_expiry_after_preparation",
            ),
            models.CheckConstraint(
                condition=Q(status="active") | Q(retired_at__isnull=False),
                name="lab_lot_retired_time",
            ),
        ]

    def __str__(self):
        return f"{self.material.name} lot {self.lot_number}"


class StockMovement(models.Model):
    """Signed change to one lot's balance. `balance_after` is the balance the writer saw."""

    class Kind(models.TextChoices):
        RECEIPT = "receipt", "Received / prepared"
        USE = "use", "Used"
        ADJUSTMENT = "adjustment", "Adjustment"
        DISPOSAL = "disposal", "Disposed"
        REVERSAL = "reversal", "Reversal"

    lot = models.ForeignKey(MaterialLot, on_delete=models.PROTECT, related_name="movements")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    delta = models.DecimalField(max_digits=18, decimal_places=6)
    balance_after = models.DecimalField(max_digits=18, decimal_places=6)
    unit = models.CharField(max_length=40)
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True)
    actor = _actor("stock_movements")
    reason = models.TextField(blank=True)
    purpose = models.CharField(max_length=200, blank=True)
    batch = models.ForeignKey(
        "analytical.Batch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="material_uses",
    )
    expired_at_use = models.BooleanField(default=False)
    reverses = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        indexes = [models.Index(fields=["lot", "-occurred_at"])]
        constraints = [
            models.CheckConstraint(condition=~Q(delta=0), name="lab_movement_nonzero"),
            models.CheckConstraint(
                condition=Q(balance_after__gte=0), name="lab_movement_balance_not_negative"
            ),
            models.CheckConstraint(
                condition=(
                    Q(kind="receipt", delta__gt=0)
                    | Q(kind="use", delta__lt=0)
                    | Q(kind="disposal", delta__lt=0)
                    | Q(kind__in=["adjustment", "reversal"])
                ),
                name="lab_movement_direction",
            ),
            models.CheckConstraint(
                condition=Q(batch__isnull=True) | Q(kind="use"), name="lab_movement_batch_is_use"
            ),
            models.CheckConstraint(
                condition=Q(kind="reversal", reverses__isnull=False)
                | (~Q(kind="reversal") & Q(reverses__isnull=True)),
                name="lab_movement_reversal_link",
            ),
        ]


# ------------------------------------------------------------------ equipment


class Equipment(models.Model):
    class Status(models.TextChoices):
        IN_SERVICE = "in_service", "In service"
        OUT_OF_SERVICE = "out_of_service", "Out of service"
        RETIRED = "retired", "Retired"

    asset_id = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    equipment_type = models.CharField(
        max_length=80,
        blank=True,
        help_text="For example balance, thermometer, ICP-MS, refrigerator.",
    )
    manufacturer = models.CharField(max_length=120, blank=True)
    model = models.CharField(max_length=120, blank=True)
    serial_number = models.CharField(max_length=120, blank=True)
    location = models.CharField(max_length=160)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.IN_SERVICE)
    notes = models.TextField(blank=True)
    revision = models.PositiveIntegerField(default=1)
    created_by = _actor()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["asset_id"]
        verbose_name_plural = "equipment"

    def __str__(self):
        return f"{self.asset_id} {self.name}"


class EquipmentEvent(EvidenceMixin):
    """Calibration or maintenance performed. The lab sets `next_due_on`; it is never computed."""

    class Kind(models.TextChoices):
        CALIBRATION = "calibration", "Calibration"
        MAINTENANCE = "maintenance", "Maintenance"

    class Outcome(models.TextChoices):
        ACCEPTABLE = "acceptable", "Acceptable"
        NOT_ACCEPTABLE = "not_acceptable", "Not acceptable"
        COMPLETED = "completed", "Completed"

    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    performed_on = models.DateField()
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    performed_by_external = models.CharField(
        "Performed by (external)",
        max_length=160,
        blank=True,
        help_text="Vendor or service provider, when not done by lab staff.",
    )
    outcome = models.CharField(max_length=14, choices=Outcome.choices)
    reference = models.CharField(
        max_length=160,
        blank=True,
        help_text="Certificate, work order, procedure or standard reference.",
    )
    description = models.TextField(blank=True, help_text="What was done, standards or parts used.")
    next_due_on = models.DateField(null=True, blank=True)
    corrects = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="corrected_by"
    )
    correction_reason = models.TextField(blank=True)
    recorded_by = _actor()
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-performed_on", "-pk"]
        indexes = [models.Index(fields=["equipment", "kind", "-performed_on"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(performed_by__isnull=False) | ~Q(performed_by_external=""),
                name="lab_equipment_event_performer",
            ),
            models.CheckConstraint(
                condition=Q(next_due_on__isnull=True) | Q(next_due_on__gte=F("performed_on")),
                name="lab_equipment_due_after_performed",
            ),
            models.CheckConstraint(
                condition=Q(corrects__isnull=True) | ~Q(correction_reason=""),
                name="lab_equipment_correction_reason",
            ),
        ]


# ------------------------------------------------------------------ training


class TrainingRecord(EvidenceMixin):
    """Training or demonstration of capability for one staff member. Informational only:
    a record never grants an application role or the report-release permission."""

    class Type(models.TextChoices):
        TRAINING = "training", "Training"
        DOC_INITIAL = "doc_initial", "Initial demonstration of capability"
        DOC_ONGOING = "doc_ongoing", "Ongoing demonstration of capability"

    class Outcome(models.TextChoices):
        ACCEPTABLE = "acceptable", "Acceptable"
        NOT_ACCEPTABLE = "not_acceptable", "Not acceptable"

    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="training_records"
    )
    record_type = models.CharField(max_length=12, choices=Type.choices)
    title = models.CharField(
        max_length=200, help_text="Training topic, SOP, or what was demonstrated."
    )
    method = models.ForeignKey(
        "catalog.Method",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    matrix = models.CharField(max_length=20, choices=MATRICES, blank=True)
    scope = models.TextField(
        blank=True, help_text="Analytes, techniques or limits of what was shown."
    )
    completed_on = models.DateField()
    effective_on = models.DateField()
    expires_on = models.DateField(
        null=True,
        blank=True,
        help_text="Set by the lab. Leave blank when the record does not expire.",
    )
    outcome = models.CharField(max_length=14, choices=Outcome.choices, default="acceptable")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="training_reviews"
    )
    reviewed_on = models.DateField()
    reference = models.CharField(
        max_length=160, blank=True, help_text="Training sheet, DOC data set or SOP identifier."
    )
    notes = models.TextField(blank=True)
    renews = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="renewals"
    )
    corrects = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="corrected_by"
    )
    correction_reason = models.TextField(blank=True)
    recorded_by = _actor()
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-effective_on", "-pk"]
        indexes = [models.Index(fields=["staff", "method", "-effective_on"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(effective_on__gte=F("completed_on")), name="lab_training_effective"
            ),
            models.CheckConstraint(
                condition=Q(expires_on__isnull=True) | Q(expires_on__gt=F("effective_on")),
                name="lab_training_expiry",
            ),
            models.CheckConstraint(
                condition=Q(record_type="training") | Q(method__isnull=False),
                name="lab_training_doc_has_method",
            ),
            models.CheckConstraint(
                condition=Q(corrects__isnull=True) | ~Q(correction_reason=""),
                name="lab_training_correction_reason",
            ),
            models.CheckConstraint(
                condition=~Q(reviewed_by=F("staff")), name="lab_training_independent_review"
            ),
        ]


# ------------------------------------------------------------------ nonconformance


class Nonconformance(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        INVESTIGATING = "investigating", "Investigating"
        ACTION = "action", "Corrective action"
        VERIFICATION = "verification", "Verification"
        CLOSED = "closed", "Closed"

    class Severity(models.TextChoices):
        MINOR = "minor", "Minor"
        MAJOR = "major", "Major"
        CRITICAL = "critical", "Critical"

    class Category(models.TextChoices):
        SAMPLE = "sample", "Sample"
        BATCH = "batch", "Batch / QC"
        EQUIPMENT = "equipment", "Equipment"
        MATERIAL = "material", "Standard / reagent"
        PERSONNEL = "personnel", "Personnel / training"
        PROCEDURE = "procedure", "Procedure / documentation"
        OTHER = "other", "Other"

    class Disposition(models.TextChoices):
        CONTINUE = "continue", "Continue work, no effect on results"
        QUALIFY = "qualify", "Continue work, qualify results"
        REPEAT = "repeat", "Repeat or re-prepare work"
        HOLD = "hold", "Hold work pending decision"
        REJECT = "reject", "Reject or discard affected work"
        OTHER = "other", "Other (explain in the note)"

    title = models.CharField(max_length=200)
    description = models.TextField()
    category = models.CharField(max_length=12, choices=Category.choices, default="other")
    severity = models.CharField(max_length=10, choices=Severity.choices)
    discovered_on = models.DateField()
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.OPEN)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="owned_nonconformances",
    )
    due_on = models.DateField(null=True, blank=True)
    sample = models.ForeignKey(
        Sample, null=True, blank=True, on_delete=models.PROTECT, related_name="nonconformances"
    )
    batch = models.ForeignKey(
        "analytical.Batch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="nonconformances",
    )
    lot = models.ForeignKey(
        MaterialLot, null=True, blank=True, on_delete=models.PROTECT, related_name="nonconformances"
    )
    equipment = models.ForeignKey(
        Equipment, null=True, blank=True, on_delete=models.PROTECT, related_name="nonconformances"
    )
    disposition = models.CharField(max_length=10, choices=Disposition.choices, blank=True)
    disposition_note = models.TextField(blank=True)
    root_cause = models.TextField(blank=True)
    corrective_action = models.TextField(blank=True)
    verification = models.TextField(blank=True)
    closure_note = models.TextField(blank=True)
    opened_by = _actor()
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-pk"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status="closed") | Q(closed_by__isnull=False, closed_at__isnull=False),
                name="lab_nc_closed_attributed",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["open"]) | Q(owner__isnull=False),
                name="lab_nc_worked_has_owner",
            ),
        ]

    @property
    def number(self):
        return f"NC-{self.pk:06d}"

    def __str__(self):
        return self.number


class NonconformanceEvent(EvidenceMixin):
    """Timeline entry. `changes` keeps prior values ({field: [before, after]}) so narrative
    fields on the parent can be revised without losing what was written earlier."""

    class Kind(models.TextChoices):
        OPENED = "opened", "Opened"
        STATUS = "status", "Status change"
        UPDATED = "updated", "Details updated"
        NOTE = "note", "Note or evidence"
        REOPENED = "reopened", "Reopened"

    nonconformance = models.ForeignKey(
        Nonconformance, on_delete=models.PROTECT, related_name="events"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    from_status = models.CharField(max_length=14, blank=True)
    to_status = models.CharField(max_length=14, blank=True)
    note = models.TextField(blank=True)
    reference = models.CharField(max_length=160, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    actor = _actor("nonconformance_events")
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["occurred_at", "pk"]
