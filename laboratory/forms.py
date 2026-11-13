"""Forms: validation for the user, then the services re-check everything. Field names that a
service reports in a ValidationError map back onto the form so input is preserved."""

import re
from decimal import Decimal

from django import forms
from django.utils import timezone

from accounts.models import User
from analytical.models import Batch
from catalog.models import Method
from core.lookups import RemoteSelect
from core.widgets import ListOrOtherField
from receipt.forms import StyledForm
from receipt.models import Sample

from .evidence import EvidenceFileField
from .models import (
    MATRICES,
    Equipment,
    EquipmentEvent,
    Material,
    MaterialLot,
    Nonconformance,
    TrainingRecord,
)
from .selectors import DAY_CHOICES

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
# Seconds are part of the value so a default "now" keeps its true order against records saved in
# the same minute; users can still enter minute precision.
DATETIME = forms.DateTimeInput(
    attrs={"type": "datetime-local", "step": "1"}, format="%Y-%m-%dT%H:%M:%S"
)


def now_local():
    """Current local time to the second, matching the precision system timestamps use."""
    return timezone.localtime().replace(microsecond=0)


def _days_or_default(value):
    """The default window (30 days) cleans to None, the same as blank: a default is not an active
    filter, so it neither shows a chip nor counts as filtering. Callers read `days or 30`."""
    days = int(value)
    return None if days == 30 else days


class DaysField(forms.TypedChoiceField):
    """Look-ahead window. Blank means the server default of 30 days, shown as such; an explicit
    30 (for example from an overview link) is accepted and means the same. Both clean to None."""

    def __init__(self, **kwargs):
        choices = [("", "30 days"), *[c for c in DAY_CHOICES if c[0] != 30]]
        super().__init__(
            required=False, coerce=_days_or_default, choices=choices, empty_value=None, **kwargs
        )

    def valid_value(self, value):
        return str(value) == "30" or super().valid_value(value)


QUANTITY = {"inputmode": "decimal", "step": "any", "class": "form-control text-end"}


def apply_errors(form, error):
    """Show a service ValidationError on the form, on the named field when it has one."""
    if hasattr(error, "message_dict"):
        for name, messages in error.message_dict.items():
            form.add_error(name if name in form.fields else None, messages)
    else:
        form.add_error(None, error)


class GroupedForm:
    groups = ()

    def grouped(self):
        return [
            (legend, [self[n] for n in names if n in self.fields])
            for legend, names in self.groups
            if any(n in self.fields for n in names)
        ]


class FilterForm(StyledForm, forms.Form):
    q = forms.CharField(required=False, max_length=200, label="Search")


class StaffChoiceField(forms.ModelChoiceField):
    def __init__(self, **kwargs):
        kwargs.setdefault(
            "queryset",
            User.objects.filter(is_active=True, client__isnull=True).order_by("username"),
        )
        kwargs.setdefault("widget", RemoteSelect("staff"))
        super().__init__(**kwargs)

    def label_from_instance(self, user):
        name = user.get_full_name()
        return f"{name} ({user.get_username()})" if name else user.get_username()


class BatchField(forms.CharField):
    """Accepts a batch number such as B-000123 or 123 and returns the Batch."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_length", 20)
        kwargs.setdefault("help_text", "Batch number, for example B-000123.")
        super().__init__(**kwargs)

    def clean(self, value):
        value = super().clean(value)
        if not value:
            return None
        match = re.fullmatch(r"(?i)\s*(?:B-?)?0*(\d+)\s*", value)
        batch = Batch.objects.filter(pk=int(match.group(1))).first() if match else None
        if batch is None:
            raise forms.ValidationError("No analytical batch has this number.")
        return batch


class SampleField(forms.CharField):
    """Accepts a laboratory sample ID and returns the Sample."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_length", 32)
        kwargs.setdefault("help_text", "Laboratory sample ID.")
        super().__init__(**kwargs)

    def clean(self, value):
        value = super().clean(value)
        if not value:
            return None
        sample = Sample.objects.filter(lab_id__iexact=value.strip()).first()
        if sample is None:
            raise forms.ValidationError("No sample has this laboratory ID.")
        return sample


class ReasonForm(GroupedForm, StyledForm, forms.Form):
    groups = (("Reason", ["reason"]),)
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), help_text="Kept in the history."
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)


# ------------------------------------------------------------------ inventory


class MaterialFilter(FilterForm):
    kind = forms.ChoiceField(required=False, choices=[("", "All kinds"), *Material.Kind.choices])
    active = forms.ChoiceField(
        required=False,
        label="Status",
        choices=[("", "All"), ("yes", "Active"), ("no", "Inactive")],
    )


class MaterialForm(GroupedForm, StyledForm, forms.Form):
    name = forms.CharField(max_length=160)
    kind = forms.ChoiceField(choices=Material.Kind.choices)
    cas_number = forms.CharField(max_length=30, required=False, label="CAS number")
    specification = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=Material._meta.get_field("specification").help_text,
    )
    unit = forms.CharField(max_length=40, help_text=Material._meta.get_field("unit").help_text)
    storage_requirements = forms.CharField(max_length=200, required=False)
    is_active = forms.BooleanField(required=False, initial=True, label="Active (new lots allowed)")
    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Required when editing. Kept in the history.",
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, required=False)
    groups = (
        ("Identity", ["name", "kind", "cas_number", "specification"]),
        ("Handling", ["unit", "storage_requirements", "is_active"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, material=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.material = material
        if material:
            self.initial.update(
                {
                    f: getattr(material, f)
                    for f in (
                        "name",
                        "kind",
                        "cas_number",
                        "specification",
                        "unit",
                        "storage_requirements",
                        "is_active",
                    )
                },
                expected_revision=material.revision,
            )
            if material.lots.exists():
                self.fields["unit"].disabled = True
                self.fields["unit"].help_text = "Fixed because lots already use it."
        else:
            del self.fields["reason"]

    def clean(self):
        data = super().clean()
        if self.material and not (data.get("reason") or "").strip():
            self.add_error("reason", "Give the reason for this change.")
        return data


class LotFilter(FilterForm):
    state = forms.ChoiceField(
        required=False,
        label="Stock",
        choices=[
            ("", "All lots"),
            ("available", "In stock, not expired"),
            ("expiring", "Expiring soon"),
            ("expired", "Expired, still on hand"),
            ("depleted", "Depleted"),
            ("retired", "Retired"),
        ],
    )
    kind = forms.ChoiceField(required=False, choices=[("", "All kinds"), *Material.Kind.choices])
    material = forms.ModelChoiceField(
        Material.objects.all(), required=False, empty_label="Any material"
    )
    days = DaysField(label="Expiring within")


class LotForm(GroupedForm, StyledForm, forms.Form):
    material = forms.ModelChoiceField(Material.objects.filter(is_active=True))
    lot_number = forms.CharField(max_length=80)
    source = forms.ChoiceField(choices=MaterialLot.Source.choices, initial="purchased")
    vendor = forms.CharField(max_length=160, required=False)
    vendor_reference = forms.CharField(
        max_length=160,
        required=False,
        label="Catalog / certificate reference",
        help_text=MaterialLot._meta.get_field("vendor_reference").help_text,
    )
    stated_concentration = forms.CharField(
        max_length=120,
        required=False,
        help_text=MaterialLot._meta.get_field("stated_concentration").help_text,
    )
    received_on = forms.DateField(required=False, widget=DATE)
    prepared_on = forms.DateField(required=False, widget=DATE)
    expires_on = forms.DateField(
        widget=DATE, help_text=MaterialLot._meta.get_field("expires_on").help_text
    )
    quantity = forms.DecimalField(
        max_digits=18,
        decimal_places=6,
        min_value=Decimal("0.000001"),
        widget=forms.NumberInput(attrs=QUANTITY),
        label="Opening quantity",
        help_text="In the material's unit.",
    )
    storage_location = forms.CharField(max_length=160)
    storage_conditions = forms.CharField(max_length=200, required=False)
    prepared_from = forms.ModelChoiceField(
        MaterialLot.objects.none(),
        required=False,
        label="Prepared from lot",
        help_text="Optional parent lot. The quantity below is drawn from it.",
    )
    parent_quantity_used = forms.DecimalField(
        required=False,
        max_digits=18,
        decimal_places=6,
        widget=forms.NumberInput(attrs=QUANTITY),
        label="Quantity drawn from parent",
        help_text="In the parent lot's unit.",
    )
    acknowledge_expired = forms.BooleanField(
        required=False,
        label="Parent lot has expired; record anyway",
    )
    groups = (
        (
            "Material and lot",
            [
                "material",
                "lot_number",
                "source",
                "vendor",
                "vendor_reference",
                "stated_concentration",
            ],
        ),
        ("Dates and quantity", ["received_on", "prepared_on", "expires_on", "quantity"]),
        ("Storage", ["storage_location", "storage_conditions"]),
        ("Preparation", ["prepared_from", "parent_quantity_used", "acknowledge_expired"]),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["material"].label_from_instance = lambda m: f"{m.name} (counted in {m.unit})"
        self.fields["prepared_from"].queryset = MaterialLot.objects.filter(
            status="active", quantity_on_hand__gt=0
        ).select_related("material")
        self.fields["prepared_from"].label_from_instance = lambda lot: (
            f"{lot.material.name} · lot {lot.lot_number} · "
            f"{lot.quantity_on_hand.normalize():f} {lot.unit}"
        )

    def clean(self):
        data = super().clean()
        if data.get("prepared_from") and not data.get("parent_quantity_used"):
            self.add_error("parent_quantity_used", "Enter the quantity drawn from the parent lot.")
        return data


class LotEditForm(GroupedForm, StyledForm, forms.Form):
    vendor = forms.CharField(max_length=160, required=False)
    vendor_reference = forms.CharField(
        max_length=160, required=False, label="Catalog / certificate reference"
    )
    stated_concentration = forms.CharField(max_length=120, required=False)
    expires_on = forms.DateField(widget=DATE)
    storage_location = forms.CharField(max_length=160)
    storage_conditions = forms.CharField(max_length=200, required=False)
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}), help_text="Kept in the history."
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    groups = (
        ("Description", ["vendor", "vendor_reference", "stated_concentration"]),
        ("Expiry and storage", ["expires_on", "storage_location", "storage_conditions"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, lot=None, **kwargs):
        super().__init__(*args, **kwargs)
        if lot:
            self.initial.update(
                {
                    f: getattr(lot, f)
                    for f in (
                        "vendor",
                        "vendor_reference",
                        "stated_concentration",
                        "expires_on",
                        "storage_location",
                        "storage_conditions",
                    )
                },
                expected_revision=lot.revision,
            )


class UseForm(GroupedForm, StyledForm, forms.Form):
    quantity = forms.DecimalField(
        max_digits=18,
        decimal_places=6,
        min_value=Decimal("0.000001"),
        widget=forms.NumberInput(attrs=QUANTITY),
    )
    occurred_at = forms.DateTimeField(widget=DATETIME, label="Used at")
    batch = BatchField(required=False, label="Analytical batch")
    purpose = forms.CharField(
        max_length=200,
        required=False,
        help_text="Required when no batch is given: what it was used for.",
    )
    acknowledge_expired = forms.BooleanField(
        required=False, label="This lot has expired; record the use anyway"
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    groups = (("Use", ["quantity", "occurred_at", "batch", "purpose", "acknowledge_expired"]),)

    def __init__(self, *args, lot=None, **kwargs):
        super().__init__(*args, **kwargs)
        if lot:
            self.fields["quantity"].label = f"Quantity used ({lot.unit})"
            self.initial.update(expected_revision=lot.revision, occurred_at=now_local())
            if lot.expires_on >= timezone.localdate():
                del self.fields["acknowledge_expired"]


class ReceiveMoreForm(GroupedForm, StyledForm, forms.Form):
    quantity = forms.DecimalField(
        max_digits=18,
        decimal_places=6,
        min_value=Decimal("0.000001"),
        widget=forms.NumberInput(attrs=QUANTITY),
    )
    occurred_at = forms.DateTimeField(widget=DATETIME, label="Received at")
    reason = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Note")
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    groups = (("Receipt", ["quantity", "occurred_at", "reason"]),)

    def __init__(self, *args, lot=None, **kwargs):
        super().__init__(*args, **kwargs)
        if lot:
            self.fields["quantity"].label = f"Quantity received ({lot.unit})"
            self.initial.update(expected_revision=lot.revision, occurred_at=now_local())


class AdjustForm(GroupedForm, StyledForm, forms.Form):
    delta = forms.DecimalField(
        max_digits=18,
        decimal_places=6,
        widget=forms.NumberInput(attrs=QUANTITY),
        label="Change in quantity",
        help_text="Negative to reduce the balance, for example -2.5.",
    )
    occurred_at = forms.DateTimeField(widget=DATETIME, label="Effective at")
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), help_text="Kept in the history."
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    groups = (("Adjustment", ["delta", "occurred_at", "reason"]),)

    def __init__(self, *args, lot=None, **kwargs):
        super().__init__(*args, **kwargs)
        if lot:
            self.fields["delta"].label = f"Change in quantity ({lot.unit})"
            self.initial.update(expected_revision=lot.revision, occurred_at=now_local())


# ------------------------------------------------------------------ equipment


class EquipmentFilter(FilterForm):
    status = forms.ChoiceField(
        required=False, choices=[("", "All statuses"), *Equipment.Status.choices]
    )
    due = forms.ChoiceField(
        required=False,
        label="Due",
        choices=[
            ("", "All"),
            ("overdue", "Overdue"),
            ("soon", "Due soon"),
            ("unscheduled", "No due date recorded"),
            ("failed", "Last event not acceptable"),
        ],
    )
    days = DaysField(label="Due within")
    equipment_type = forms.CharField(required=False, label="Type", max_length=80)
    location = forms.CharField(required=False, max_length=160)


# Starter list; "Other" accepts any type, and types typed before the list stay valid.
EQUIPMENT_TYPES = (
    "Balance",
    "Thermometer",
    "Infrared thermometer",
    "pH meter",
    "Conductivity meter",
    "Dissolved oxygen meter",
    "Turbidimeter",
    "Spectrophotometer",
    "ICP-MS",
    "ICP-OES",
    "Mercury analyzer",
    "Ion chromatograph",
    "Autotitrator",
    "Digestion block",
    "Drying oven",
    "Incubator",
    "Refrigerator",
    "Freezer",
    "Pipette",
    "Water purification system",
)


class EquipmentForm(GroupedForm, StyledForm, forms.Form):
    asset_id = forms.CharField(max_length=40, label="Asset ID")
    name = forms.CharField(max_length=160)
    equipment_type = ListOrOtherField(
        EQUIPMENT_TYPES,
        max_length=80,
        required=False,
        help_text="Thermometers listed here are suggested when a receipt temperature is recorded.",
    )
    manufacturer = forms.CharField(max_length=120, required=False)
    model = forms.CharField(max_length=120, required=False)
    serial_number = forms.CharField(max_length=120, required=False)
    location = forms.CharField(max_length=160)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Required when editing. Kept in the history.",
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, required=False)
    groups = (
        (
            "Identity",
            ["asset_id", "name", "equipment_type", "manufacturer", "model", "serial_number"],
        ),
        ("Placement", ["location", "notes"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, equipment=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.equipment = equipment
        if equipment:
            self.initial.update(
                {
                    f: getattr(equipment, f)
                    for f in (
                        "asset_id",
                        "name",
                        "equipment_type",
                        "manufacturer",
                        "model",
                        "serial_number",
                        "location",
                        "notes",
                    )
                },
                expected_revision=equipment.revision,
            )
        else:
            del self.fields["reason"]

    def clean(self):
        data = super().clean()
        if self.equipment and not (data.get("reason") or "").strip():
            self.add_error("reason", "Give the reason for this change.")
        return data


class StatusForm(GroupedForm, StyledForm, forms.Form):
    groups = (("Status", ["status", "reason"]),)
    status = forms.ChoiceField(choices=Equipment.Status.choices)
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), help_text="Kept in the history."
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, equipment=None, **kwargs):
        super().__init__(*args, **kwargs)
        if equipment:
            self.initial.update(status=equipment.status, expected_revision=equipment.revision)


class EquipmentEventForm(GroupedForm, StyledForm, forms.Form):
    kind = forms.ChoiceField(choices=EquipmentEvent.Kind.choices)
    performed_on = forms.DateField(widget=DATE)
    performed_by = StaffChoiceField(required=False, label="Performed by (staff)")
    performed_by_external = forms.CharField(
        max_length=160, required=False, label="Performed by (outside provider)"
    )
    outcome = forms.ChoiceField(choices=EquipmentEvent.Outcome.choices)
    reference = forms.CharField(
        max_length=160,
        required=False,
        help_text=EquipmentEvent._meta.get_field("reference").help_text,
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=EquipmentEvent._meta.get_field("description").help_text,
    )
    next_due_on = forms.DateField(
        required=False,
        widget=DATE,
        label="Next due",
        help_text="Set by the lab. Leave blank if no next date is scheduled.",
    )
    evidence = EvidenceFileField()
    correction_reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Why the earlier record is being corrected. Kept in the history.",
    )
    groups = (
        ("Event", ["kind", "performed_on", "performed_by", "performed_by_external", "outcome"]),
        ("Record", ["reference", "description", "next_due_on", "evidence"]),
        ("Correction", ["correction_reason"]),
    )

    def __init__(self, *args, corrects=None, kind=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.corrects = corrects
        if corrects:
            self.initial.update(
                kind=corrects.kind,
                performed_on=corrects.performed_on,
                performed_by=corrects.performed_by_id,
                performed_by_external=corrects.performed_by_external,
                outcome=corrects.outcome,
                reference=corrects.reference,
                description=corrects.description,
                next_due_on=corrects.next_due_on,
            )
            self.fields["kind"].disabled = True
        else:
            del self.fields["correction_reason"]
            if kind in EquipmentEvent.Kind.values:
                self.initial["kind"] = kind
            self.initial.setdefault("performed_on", timezone.localdate())

    def clean(self):
        data = super().clean()
        if self.corrects and not (data.get("correction_reason") or "").strip():
            self.add_error("correction_reason", "Give the reason for this correction.")
        return data


# ------------------------------------------------------------------ training


class TrainingFilter(FilterForm):
    state = forms.ChoiceField(
        required=False,
        label="State",
        choices=[
            ("", "All records"),
            ("current", "Current"),
            ("attention", "Expiring or expired"),
            ("pending", "Not yet effective"),
            ("not_acceptable", "Not acceptable"),
            ("corrected", "Corrected"),
        ],
    )
    days = DaysField(label="Expiring within")
    record_type = forms.ChoiceField(
        required=False, label="Type", choices=[("", "All types"), *TrainingRecord.Type.choices]
    )
    method = forms.ModelChoiceField(Method.objects.all(), required=False, empty_label="Any method")
    staff = StaffChoiceField(required=False, empty_label="Any person")


class TrainingForm(GroupedForm, StyledForm, forms.Form):
    staff = StaffChoiceField(label="Staff member")
    record_type = forms.ChoiceField(choices=TrainingRecord.Type.choices, label="Type")
    title = forms.CharField(
        max_length=200, help_text=TrainingRecord._meta.get_field("title").help_text
    )
    method = forms.ModelChoiceField(
        Method.objects.all(),
        required=False,
        help_text="Required for a demonstration of capability.",
    )
    matrix = forms.ChoiceField(required=False, choices=[("", "Not specific"), *MATRICES])
    scope = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=TrainingRecord._meta.get_field("scope").help_text,
    )
    outcome = forms.ChoiceField(choices=TrainingRecord.Outcome.choices)
    completed_on = forms.DateField(widget=DATE)
    effective_on = forms.DateField(widget=DATE)
    expires_on = forms.DateField(
        required=False,
        widget=DATE,
        help_text=TrainingRecord._meta.get_field("expires_on").help_text,
    )
    reviewed_by = StaffChoiceField(label="Reviewed by")
    reviewed_on = forms.DateField(widget=DATE)
    reference = forms.CharField(
        max_length=160,
        required=False,
        help_text=TrainingRecord._meta.get_field("reference").help_text,
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    evidence = EvidenceFileField()
    correction_reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Why the earlier record is being corrected. Kept in the history.",
    )
    groups = (
        ("Person and subject", ["staff", "record_type", "title", "method", "matrix", "scope"]),
        ("Dates and outcome", ["outcome", "completed_on", "effective_on", "expires_on"]),
        ("Review and evidence", ["reviewed_by", "reviewed_on", "reference", "evidence", "notes"]),
        ("Correction", ["correction_reason"]),
    )

    def __init__(self, *args, renews=None, corrects=None, **kwargs):
        super().__init__(*args, **kwargs)
        source = corrects or renews
        self.renews, self.corrects = renews, corrects
        if source:
            self.initial.update(
                staff=source.staff_id,
                record_type=source.record_type,
                title=source.title,
                method=source.method_id,
                matrix=source.matrix,
                scope=source.scope,
                reviewed_by=source.reviewed_by_id,
            )
            for name in ("staff", "record_type", "method"):
                self.fields[name].disabled = True
        if corrects:
            self.initial.update(
                outcome=corrects.outcome,
                completed_on=corrects.completed_on,
                effective_on=corrects.effective_on,
                expires_on=corrects.expires_on,
                reviewed_on=corrects.reviewed_on,
                reference=corrects.reference,
                notes=corrects.notes,
            )
        else:
            del self.fields["correction_reason"]
            self.initial.setdefault("reviewed_on", timezone.localdate())

    def clean(self):
        data = super().clean()
        if self.corrects and not (data.get("correction_reason") or "").strip():
            self.add_error("correction_reason", "Give the reason for this correction.")
        return data


# ------------------------------------------------------------------ nonconformance


class NonconformanceFilter(FilterForm):
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All statuses"),
            ("not_closed", "Not closed"),
            *Nonconformance.Status.choices,
        ],
    )
    severity = forms.ChoiceField(
        required=False, choices=[("", "All severities"), *Nonconformance.Severity.choices]
    )
    category = forms.ChoiceField(
        required=False, choices=[("", "All categories"), *Nonconformance.Category.choices]
    )
    owner = forms.ChoiceField(
        required=False,
        choices=[("", "Any owner"), ("me", "Assigned to me"), ("none", "Unassigned")],
    )
    overdue = forms.ChoiceField(
        required=False, choices=[("", "All"), ("yes", "Overdue only")], label="Due"
    )


class NonconformanceForm(GroupedForm, StyledForm, forms.Form):
    title = forms.CharField(max_length=200)
    description = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4}), help_text="What happened and how it was found."
    )
    category = forms.ChoiceField(
        choices=[("", "Choose a category"), *Nonconformance.Category.choices]
    )
    severity = forms.ChoiceField(
        choices=[("", "Choose a severity"), *Nonconformance.Severity.choices]
    )
    discovered_on = forms.DateField(widget=DATE)
    owner = StaffChoiceField(required=False, help_text="Required to start the investigation.")
    due_on = forms.DateField(required=False, widget=DATE, label="Due")
    sample = SampleField(required=False)
    batch = BatchField(required=False, label="Analytical batch")
    lot = forms.ModelChoiceField(
        MaterialLot.objects.none(),
        required=False,
        label="Standard / reagent lot",
        widget=RemoteSelect("lots"),
    )
    equipment = forms.ModelChoiceField(
        Equipment.objects.all(), required=False, widget=RemoteSelect("equipment")
    )
    reference = forms.CharField(
        max_length=160, required=False, help_text="Report, ticket or log reference."
    )
    evidence = EvidenceFileField()
    groups = (
        ("Issue", ["title", "description", "category", "severity", "discovered_on"]),
        ("Linked records", ["sample", "batch", "lot", "equipment"]),
        ("Ownership", ["owner", "due_on"]),
        ("Evidence", ["reference", "evidence"]),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = MaterialLot.objects.select_related("material").order_by(
            "material__name", "lot_number"
        )
        self.initial.setdefault("discovered_on", timezone.localdate())


class NonconformanceEditForm(GroupedForm, StyledForm, forms.Form):
    title = forms.CharField(max_length=200)
    category = forms.ChoiceField(choices=Nonconformance.Category.choices)
    severity = forms.ChoiceField(choices=Nonconformance.Severity.choices)
    owner = StaffChoiceField(required=False)
    due_on = forms.DateField(required=False, widget=DATE, label="Due")
    sample = SampleField(required=False)
    batch = BatchField(required=False, label="Analytical batch")
    lot = forms.ModelChoiceField(
        MaterialLot.objects.none(),
        required=False,
        label="Standard / reagent lot",
        widget=RemoteSelect("lots"),
    )
    equipment = forms.ModelChoiceField(
        Equipment.objects.all(), required=False, widget=RemoteSelect("equipment")
    )
    disposition = forms.ChoiceField(
        required=False, choices=[("", "Not decided"), *Nonconformance.Disposition.choices]
    )
    disposition_note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    root_cause = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    corrective_action = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Why this is being changed. Earlier text stays in the timeline.",
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    groups = (
        ("Issue", ["title", "category", "severity"]),
        ("Ownership", ["owner", "due_on"]),
        ("Linked records", ["sample", "batch", "lot", "equipment"]),
        ("Findings", ["disposition", "disposition_note", "root_cause", "corrective_action"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, nc=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = MaterialLot.objects.select_related("material").order_by(
            "material__name", "lot_number"
        )
        if nc:
            self.initial.update(
                {
                    f: getattr(nc, f)
                    for f in (
                        "title",
                        "category",
                        "severity",
                        "due_on",
                        "disposition",
                        "disposition_note",
                        "root_cause",
                        "corrective_action",
                    )
                },
                owner=nc.owner_id,
                lot=nc.lot_id,
                equipment=nc.equipment_id,
                sample=nc.sample.lab_id if nc.sample_id else "",
                batch=nc.batch.number if nc.batch_id else "",
                expected_revision=nc.revision,
            )


class StepForm(GroupedForm, StyledForm, forms.Form):
    """One form for every forward step; only the fields that step needs are kept."""

    owner = StaffChoiceField(required=False)
    due_on = forms.DateField(required=False, widget=DATE, label="Due")
    root_cause = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
    disposition = forms.ChoiceField(
        required=False,
        choices=[("", "Choose a disposition"), *Nonconformance.Disposition.choices],
        help_text="What happens to the affected work.",
    )
    disposition_note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    corrective_action = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Action taken to correct the cause and prevent recurrence.",
    )
    verification = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="How the corrective action was shown to work.",
    )
    closure_note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Note")
    reference = forms.CharField(max_length=160, required=False)
    evidence = EvidenceFileField()
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)

    FIELDS = {
        "investigating": ["owner", "due_on", "note"],
        "action": ["root_cause", "disposition", "disposition_note", "due_on", "note"],
        "verification": ["corrective_action", "due_on", "note"],
        "closed": ["verification", "closure_note", "reference", "evidence", "note"],
    }
    REASON_ONLY = {"back": ["note"], "reopen": ["note"]}

    def __init__(self, *args, nc, step, **kwargs):
        super().__init__(*args, **kwargs)
        keep = self.FIELDS.get(step) or self.REASON_ONLY[step]
        if step in self.REASON_ONLY:
            self.fields["note"].label = "Reason"
            self.fields["note"].required = True
            self.fields["note"].help_text = "Kept in the timeline."
        if step in {"action", "verification", "closed"}:
            self.fields["note"].label = "Note"
        if step == "action":
            self.fields["evidence"].required = False
        for name in list(self.fields):
            if name not in keep and name != "expected_revision":
                del self.fields[name]
        self.initial["expected_revision"] = nc.revision
        for name in (
            "owner",
            "due_on",
            "root_cause",
            "disposition",
            "disposition_note",
            "corrective_action",
            "verification",
            "closure_note",
        ):
            if name in self.fields:
                value = getattr(nc, name + "_id") if name == "owner" else getattr(nc, name)
                self.initial[name] = value
        if step == "investigating":
            self.fields["owner"].required = True
        self.step = step

    def grouped(self):
        return [("", [self[n] for n in self.fields if n != "expected_revision"])]


class NoteForm(GroupedForm, StyledForm, forms.Form):
    groups = (("Note or evidence", ["note", "reference", "evidence"]),)
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    reference = forms.CharField(max_length=160, required=False)
    evidence = EvidenceFileField()


class LookAheadForm(forms.Form):
    """Lab overview's look-ahead window: a display setting, not lab policy."""

    days = forms.TypedChoiceField(label="Look ahead", choices=DAY_CHOICES, coerce=int)

    def __init__(self, days, **kwargs):
        super().__init__(auto_id="ov-%s", initial={"days": days}, **kwargs)
