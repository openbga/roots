import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from .vocabulary import US_STATES


class Client(models.Model):
    class PaymentTerms(models.TextChoices):
        DUE_ON_RECEIPT = "due_on_receipt", "Due on receipt"
        NET_15 = "net_15", "Net 15 days"
        NET_30 = "net_30", "Net 30 days"
        NET_45 = "net_45", "Net 45 days"
        NET_60 = "net_60", "Net 60 days"
        PREPAID = "prepaid", "Prepaid"

    name = models.CharField(max_length=160)
    # New text columns carry db_default so the previous release's inserts stay valid while
    # old and new instances overlap during a deploy.
    account_number = models.CharField(
        "Account code",
        max_length=40,
        blank=True,
        db_default="",
        help_text="The laboratory's short code for this client. Unique when given.",
    )
    contact_name = models.CharField(max_length=160, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True, db_default="")
    address_line1 = models.CharField("Street address", max_length=160, blank=True, db_default="")
    address_line2 = models.CharField(
        "Suite, building or PO box", max_length=160, blank=True, db_default=""
    )
    city = models.CharField(max_length=80, blank=True, db_default="")
    state = models.CharField(max_length=2, blank=True, db_default="", choices=US_STATES)
    postal_code = models.CharField("ZIP code", max_length=12, blank=True, db_default="")
    billing_contact_name = models.CharField(
        "Invoice-to contact", max_length=160, blank=True, db_default=""
    )
    billing_email = models.EmailField("Invoice email", blank=True, db_default="")
    billing_address = models.TextField(
        blank=True,
        db_default="",
        help_text="Leave blank when invoices go to the mailing address.",
    )
    payment_terms = models.CharField(
        max_length=16, blank=True, db_default="", choices=PaymentTerms.choices
    )
    po_required = models.BooleanField(
        "Purchase order required",
        default=False,
        db_default=False,
        help_text="Information for intake: a chain of custody for this client should carry a PO.",
    )
    report_preferences = models.TextField(blank=True)
    notes = models.TextField(
        blank=True, db_default="", help_text="Internal notes. Not shown to the client."
    )
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["account_number"],
                condition=~models.Q(account_number=""),
                name="client_account_number",
                violation_error_message="Another client already uses this account code.",
            )
        ]

    def __str__(self):
        return self.name

    @property
    def address_lines(self):
        """Mailing address as display lines; empty when no part is recorded."""
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


class ClientContact(models.Model):
    """An additional person at a client. The client's own contact name and email stay the primary
    contact; this table adds billing, sampling or report contacts with phone numbers."""

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="contacts")
    name = models.CharField(max_length=160)
    role = models.CharField(
        max_length=80, blank=True, help_text="For example billing, sampler or project lead."
    )
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    receives_reports = models.BooleanField(
        default=False,
        help_text="Who the client wants reports sent to. Information only; nothing is emailed.",
    )
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-is_active", "name", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(email="") | ~models.Q(phone=""),
                name="client_contact_reachable",
                violation_error_message="Give an email address or a phone number.",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.client})"


class Project(models.Model):
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="projects")

    class RegulatoryProgram(models.TextChoices):
        NPDES = "npdes", "NPDES / Clean Water Act"
        PRETREATMENT = "pretreatment", "Industrial pretreatment"
        STORMWATER = "stormwater", "Stormwater permit"
        DRINKING_WATER = "drinking_water", "Drinking water (SDWA)"
        RCRA = "rcra", "RCRA / waste characterization"
        REMEDIATION = "remediation", "Site investigation / remediation"
        STATE = "state", "State program"
        NONE = "none", "Not regulatory"
        OTHER = "other", "Other"

    name = models.CharField(max_length=160)
    number = models.CharField(
        "Project number",
        max_length=60,
        blank=True,
        db_default="",
        help_text="The client's or the laboratory's reference for this project.",
    )
    regulatory_program = models.CharField(
        max_length=16, blank=True, db_default="", choices=RegulatoryProgram.choices
    )
    program = models.CharField("Permit or program ID", max_length=160, blank=True)
    site_address = models.CharField(
        "Site or facility",
        max_length=200,
        blank=True,
        db_default="",
        help_text="Facility name or address where samples are collected.",
    )
    site_state = models.CharField(
        "State of sample origin", max_length=2, blank=True, db_default="", choices=US_STATES
    )
    po_number = models.CharField(
        "Purchase order",
        max_length=60,
        blank=True,
        db_default="",
        help_text="Standing PO. A new chain of custody without its own PO takes this one.",
    )
    project_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="managed_projects",
        verbose_name="Laboratory project manager",
    )
    turnaround_days = models.PositiveSmallIntegerField(default=10)
    instructions = models.TextField(
        "Standing instructions",
        blank=True,
        db_default="",
        help_text="Shown to the custodian when receiving samples for this project.",
    )
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return f"{self.client} / {self.name}"


class Order(models.Model):
    class Priority(models.TextChoices):
        STANDARD = "standard", "Standard"
        RUSH = "rush", "Rush"

    class DeliveryMethod(models.TextChoices):
        CLIENT = "client", "Client drop-off"
        LAB_COURIER = "lab_courier", "Laboratory courier / pickup"
        CARRIER = "carrier", "Commercial carrier"
        MAIL = "mail", "Mail"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="orders")
    coc_reference = models.CharField(max_length=120)
    due_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Agreed delivery target in the laboratory time zone; not a holding-time limit.",
    )
    po_number = models.CharField("Purchase order", max_length=60, blank=True, db_default="")
    priority = models.CharField(
        "Turnaround",
        max_length=10,
        choices=Priority.choices,
        default=Priority.STANDARD,
        db_default=Priority.STANDARD,
        help_text="Rush marks the work in lists; the delivery target stays a separate date.",
    )
    delivery_method = models.CharField(
        max_length=12, blank=True, db_default="", choices=DeliveryMethod.choices
    )
    carrier_tracking = models.CharField(
        "Carrier / tracking number", max_length=160, blank=True, db_default=""
    )
    cooler_count = models.PositiveSmallIntegerField(
        "Coolers / packages",
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(200)],
    )
    instructions = models.TextField(
        "Special instructions",
        blank=True,
        db_default="",
        help_text="Rush approval, hazards, report or invoicing instructions for this shipment.",
    )
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    # Set when an expected shipment never arrives. Only an order with no received sample can be
    # cancelled, and the cancellation can be reversed with its own reason; history is kept.
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # db_default keeps the previous release's inserts valid while this column exists.
    cancel_reason = models.TextField(blank=True, db_default="")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(cancelled_at__isnull=True, cancelled_by__isnull=True)
                | (
                    models.Q(cancelled_at__isnull=False, cancelled_by__isnull=False)
                    & ~models.Q(cancel_reason="")
                ),
                name="order_cancellation_attributed",
            )
        ]

    @property
    def is_cancelled(self):
        return self.cancelled_at is not None

    @property
    def is_rush(self):
        return self.priority == self.Priority.RUSH


class Sample(models.Model):
    class Matrix(models.TextChoices):
        WATER = "water", "Water"
        WASTEWATER = "wastewater", "Wastewater"
        SOIL = "soil", "Soil"

    class Disposition(models.TextChoices):
        PROCEED = "proceed", "Proceed"
        QUALIFY = "qualify", "Qualify"
        HOLD = "hold", "Hold"
        REJECT = "reject", "Reject"

    class SampleType(models.TextChoices):
        """Field designation as written on the chain of custody. Laboratory QC samples
        (blanks, spikes, duplicates prepared in the lab) belong to analytical batches."""

        SAMPLE = "sample", "Field sample"
        FIELD_DUPLICATE = "field_duplicate", "Field duplicate"
        FIELD_BLANK = "field_blank", "Field blank"
        TRIP_BLANK = "trip_blank", "Trip blank"
        EQUIPMENT_BLANK = "equipment_blank", "Equipment / rinsate blank"

    class Source(models.TextChoices):
        """What was sampled, within the analytical matrix (see SOURCES_BY_MATRIX)."""

        DRINKING_WATER = "drinking_water", "Drinking water"
        GROUNDWATER = "groundwater", "Groundwater"
        SURFACE_WATER = "surface_water", "Surface water"
        STORMWATER = "stormwater", "Stormwater"
        INFLUENT = "influent", "Wastewater influent"
        EFFLUENT = "effluent", "Wastewater effluent"
        INDUSTRIAL = "industrial", "Industrial wastewater"
        SOIL = "soil", "Soil"
        SEDIMENT = "sediment", "Sediment"
        SLUDGE = "sludge", "Sludge / biosolids"
        SOLID_WASTE = "solid_waste", "Solid waste"

    class Coolant(models.TextChoices):
        WET_ICE = "wet_ice", "Wet ice"
        ICE_PACKS = "ice_packs", "Ice packs"
        DRY_ICE = "dry_ice", "Dry ice"
        NONE = "none", "None"

    SOURCES_BY_MATRIX = {
        "water": ("drinking_water", "groundwater", "surface_water", "stormwater"),
        "wastewater": ("influent", "effluent", "industrial", "stormwater"),
        "soil": ("soil", "sediment", "sludge", "solid_waste"),
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lab_id = models.CharField(max_length=32, unique=True)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="samples")
    client_sample_id = models.CharField(max_length=120)
    matrix = models.CharField(max_length=20, choices=Matrix.choices)
    source_type = models.CharField(
        "Sample source", max_length=20, blank=True, db_default="", choices=Source.choices
    )
    sample_type = models.CharField(
        max_length=20,
        choices=SampleType.choices,
        default=SampleType.SAMPLE,
        db_default=SampleType.SAMPLE,
    )
    collection_type = models.CharField(
        max_length=20, choices=[("grab", "Grab"), ("composite", "Composite")], default="grab"
    )
    collected_at = models.DateTimeField(help_text="Collection time, or composite collection end.")
    collection_started_at = models.DateTimeField(null=True, blank=True)
    collector_name = models.CharField(max_length=160)
    collection_location = models.CharField(max_length=200, blank=True)
    remarks = models.TextField(
        "Sample remarks",
        blank=True,
        db_default="",
        help_text="Appearance, odor, hazards or field readings noted on the chain of custody.",
    )
    received_at = models.DateTimeField()
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    cooler_temperature = models.DecimalField(max_digits=7, decimal_places=3)
    cooler_temperature_original = models.CharField(max_length=80, blank=True)
    cooler_temperature_unit = models.CharField(max_length=8, default="°C", editable=False)
    temperature_instrument = models.CharField(max_length=160, blank=True)
    coolant = models.CharField(
        "Coolant on arrival", max_length=12, blank=True, db_default="", choices=Coolant.choices
    )
    seal_condition = models.CharField(max_length=160)
    preservation_check = models.CharField(max_length=200)
    discrepancy = models.TextField(blank=True)
    disposition = models.CharField(max_length=20, choices=Disposition.choices, default="proceed")
    disposition_note = models.TextField(blank=True)
    current_location = models.CharField(max_length=160)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-received_at", "lab_id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(received_at__gte=models.F("collected_at")),
                name="receipt_after_collection",
            ),
            models.CheckConstraint(
                condition=models.Q(collection_started_at__isnull=True)
                | models.Q(collection_started_at__lte=models.F("collected_at")),
                name="collection_order",
            ),
        ]

    def __str__(self):
        return self.lab_id

    @classmethod
    def source_matches(cls, matrix, source_type):
        return not source_type or source_type in cls.SOURCES_BY_MATRIX.get(matrix, ())

    @classmethod
    def source_choices(cls):
        """Sources grouped by matrix for a select; stormwater is offered under both waters."""
        labels = dict(cls.Source.choices)
        return [
            (cls.Matrix(matrix).label, [(value, labels[value]) for value in values])
            for matrix, values in cls.SOURCES_BY_MATRIX.items()
        ]


class Container(models.Model):
    sample = models.ForeignKey(Sample, on_delete=models.PROTECT, related_name="containers")
    label = models.CharField(max_length=120)
    description = models.CharField(max_length=160)
    condition = models.CharField(max_length=160)
    preservation = models.CharField(max_length=200, blank=True)


class RequestedTest(models.Model):
    sample = models.ForeignKey(Sample, on_delete=models.PROTECT, related_name="requested_tests")
    definition = models.ForeignKey("catalog.TestDefinition", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["sample", "definition"], name="sample_requested_test")
        ]


class CustodyEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sample = models.ForeignKey(Sample, on_delete=models.PROTECT, related_name="custody_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(auto_now_add=True)
    occurred_at = models.DateTimeField()
    from_location = models.CharField(max_length=160, blank=True)
    to_location = models.CharField(max_length=160)
    relinquished_by = models.CharField(max_length=160)
    received_by = models.CharField(max_length=160)
    reason = models.TextField()
    kind = models.CharField(
        max_length=20,
        choices=[("receipt", "Receipt"), ("transfer", "Transfer"), ("disposal", "Disposal")],
    )

    class Meta:
        ordering = ["occurred_at", "recorded_at", "id"]


from .storage_models import SampleStorage  # noqa: E402, F401
from .subcontract_models import Subcontract, SubcontractResult  # noqa: E402, F401
