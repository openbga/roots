"""Commercial intake: price lists, versioned quotes and client-submitted chains of custody.

A submission is the client's declared intent. Physical receipt, temperature, condition and custody
stay in `receipt`; `SubmissionSample.sample` is set only when a qualified user receives the sample.
Money is Decimal(USD) end to end; issued quote versions and submitted snapshots are never edited.
"""

import uuid

from django.conf import settings
from django.db import models

from catalog.models import TestDefinition
from receipt.models import Client, Order, Project, Sample

MONEY = {"max_digits": 12, "decimal_places": 2}
MISSING_TITLE = "Title not recorded"
MISSING_CLIENT = "Client not recorded"


class PriceList(models.Model):
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class PriceListItem(models.Model):
    price_list = models.ForeignKey(PriceList, on_delete=models.PROTECT, related_name="items")
    test = models.ForeignKey(TestDefinition, on_delete=models.PROTECT, related_name="prices")
    unit_price = models.DecimalField(**MONEY, help_text="US dollars per sample.")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["price_list", "test"], name="price_list_test"),
            models.CheckConstraint(
                condition=models.Q(unit_price__gte=0), name="price_not_negative"
            ),
        ]


class Quote(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        REVISING = "revising", "Being revised"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        WITHDRAWN = "withdrawn", "Withdrawn"

    number = models.CharField(max_length=20, unique=True, blank=True)
    token = models.UUIDField(null=True, unique=True, editable=False)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="quotes")
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.PROTECT, related_name="quotes"
    )
    title = models.CharField(max_length=160)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    # Client decision on one issued version.
    decision_version = models.PositiveSmallIntegerField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decision_note = models.TextField(blank=True)
    client_reference = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-pk"]

    def __str__(self):
        return self.number


class QuoteVersion(models.Model):
    quote = models.ForeignKey(Quote, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveSmallIntegerField()
    price_list = models.ForeignKey(PriceList, null=True, blank=True, on_delete=models.PROTECT)
    valid_until = models.DateField(null=True, blank=True)
    turnaround_days = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Standard turnaround in business days from lab receipt."
    )
    scope_notes = models.TextField(blank=True)
    terms = models.TextField(blank=True)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # Presentation frozen at issue time (blank on drafts). An issued version never falls back to
    # the live quote, client or project; an empty project_name means the quote had no project.
    title = models.CharField(max_length=160, blank=True)
    client_name = models.CharField(max_length=160, blank=True)
    project_name = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    issued_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["quote_id", "number"]
        constraints = [
            models.UniqueConstraint(fields=["quote", "number"], name="quote_version_number"),
            models.CheckConstraint(
                condition=models.Q(issued_at__isnull=True, issued_by__isnull=True)
                | models.Q(issued_at__isnull=False, issued_by__isnull=False),
                name="quote_issue_actor_time",
            ),
            models.CheckConstraint(
                condition=models.Q(total__gte=0), name="quote_total_not_negative"
            ),
        ]

    @property
    def is_issued(self):
        return self.issued_at is not None

    # Issued versions present only what was frozen at issue; a draft shows the live quote so
    # staff see what they are editing. A project is optional, so a frozen empty project is valid.
    @property
    def presented_title(self):
        if self.is_issued:
            return self.title or MISSING_TITLE
        return self.quote.title

    @property
    def presented_client(self):
        if self.is_issued:
            return self.client_name or MISSING_CLIENT
        return self.quote.client.name

    @property
    def presented_project(self):
        if self.is_issued:
            return self.project_name
        return self.quote.project.name if self.quote.project_id else ""


class QuoteLine(models.Model):
    class Kind(models.TextChoices):
        TEST = "test", "Test"
        FEE = "fee", "Fee"
        ADJUSTMENT = "adjustment", "Discount / adjustment"

    version = models.ForeignKey(QuoteVersion, on_delete=models.PROTECT, related_name="lines")
    position = models.PositiveSmallIntegerField(default=0)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    test = models.ForeignKey(
        TestDefinition, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    description = models.CharField(max_length=240)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(**MONEY)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["version_id", "position", "pk"]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gte=1), name="quote_line_quantity"),
            models.CheckConstraint(
                condition=models.Q(amount=models.F("quantity") * models.F("unit_price")),
                name="quote_line_amount",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="test", test__isnull=False, unit_price__gte=0)
                    | models.Q(kind="fee", unit_price__gte=0)
                    | models.Q(kind="adjustment", unit_price__lte=0)
                ),
                name="quote_line_kind_price",
            ),
        ]


class Submission(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        RETURNED = "returned", "Returned for changes"
        ACCEPTED = "accepted", "Accepted"
        CANCELLED = "cancelled", "Cancelled"

    number = models.CharField(max_length=20, unique=True, blank=True)
    token = models.UUIDField(null=True, unique=True, editable=False)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="submissions")
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="submissions")
    quote = models.ForeignKey(
        Quote, null=True, blank=True, on_delete=models.PROTECT, related_name="submissions"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    revision = models.PositiveIntegerField(default=1)
    coc_reference = models.CharField(max_length=120, verbose_name="Chain of custody reference")
    expected_arrival = models.DateField(null=True, blank=True)
    carrier = models.CharField(max_length=160, blank=True, verbose_name="Carrier / tracking")
    cooler_count = models.PositiveSmallIntegerField(null=True, blank=True)
    contact_name = models.CharField(max_length=160, blank=True)
    contact_phone = models.CharField(max_length=40, blank=True)
    special_instructions = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    order = models.OneToOneField(
        Order, null=True, blank=True, on_delete=models.PROTECT, related_name="submission"
    )

    class Meta:
        ordering = ["-pk"]

    def __str__(self):
        return self.number


class SubmissionSample(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="samples")
    position = models.PositiveSmallIntegerField(default=0)
    client_sample_id = models.CharField(max_length=120)
    matrix = models.CharField(max_length=20, choices=Sample.Matrix.choices)
    # Declared by the client with the same lists the laboratory records at receipt.
    source_type = models.CharField(
        "Sample source", max_length=20, blank=True, db_default="", choices=Sample.Source.choices
    )
    sample_type = models.CharField(
        max_length=20,
        choices=Sample.SampleType.choices,
        default=Sample.SampleType.SAMPLE,
        db_default=Sample.SampleType.SAMPLE,
    )
    collection_type = models.CharField(
        max_length=20, choices=[("grab", "Grab"), ("composite", "Composite")], default="grab"
    )
    collection_started_at = models.DateTimeField(null=True, blank=True)
    # Blank collected_at means "not collected yet": the client is preparing bottles. Only a real
    # collection is ever recorded here; a plan goes in planned_collection_on.
    collected_at = models.DateTimeField(
        null=True, blank=True, help_text="Collection time, or composite collection end."
    )
    planned_collection_on = models.DateField(
        null=True, blank=True, help_text="Expected collection date, while not yet collected."
    )
    collector_name = models.CharField(max_length=160)
    collection_location = models.CharField(max_length=200)
    field_notes = models.TextField(blank=True)
    tests = models.ManyToManyField(TestDefinition, related_name="+")
    # Set only when the laboratory physically receives this sample.
    sample = models.OneToOneField(
        Sample, null=True, blank=True, on_delete=models.PROTECT, related_name="submission_sample"
    )

    class Meta:
        ordering = ["submission_id", "position", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "client_sample_id"], name="submission_client_sample_id"
            ),
            models.CheckConstraint(
                condition=models.Q(collection_started_at__isnull=True)
                | models.Q(
                    collected_at__isnull=False,
                    collection_started_at__lte=models.F("collected_at"),
                ),
                name="submission_collection_order",
            ),
        ]

    @property
    def is_collected(self):
        return self.collected_at is not None

    @property
    def container_total(self):
        return sum(c.quantity for c in self.containers.all())


class SubmissionContainer(models.Model):
    sample = models.ForeignKey(
        SubmissionSample, on_delete=models.CASCADE, related_name="containers"
    )
    description = models.CharField(max_length=160, help_text="For example 1 L amber glass.")
    preservation = models.CharField(max_length=200, blank=True)
    quantity = models.PositiveSmallIntegerField(default=1)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1, quantity__lte=20), name="container_quantity"
            )
        ]


class SubmissionSnapshot(models.Model):
    """Exactly what the client submitted, frozen, with the laboratory's decision on it."""

    class Outcome(models.TextChoices):
        PENDING = "", "Awaiting review"
        RETURNED = "returned", "Returned"
        ACCEPTED = "accepted", "Accepted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="snapshots")
    number = models.PositiveSmallIntegerField()
    submitted_at = models.DateTimeField()
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    data = models.JSONField()
    outcome = models.CharField(max_length=12, choices=Outcome.choices, blank=True, default="")
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decision_note = models.TextField(blank=True)

    class Meta:
        ordering = ["submission_id", "number"]
        constraints = [
            models.UniqueConstraint(fields=["submission", "number"], name="submission_snapshot_no")
        ]
