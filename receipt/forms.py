from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone

from catalog.models import TestDefinition
from core.lookups import RemoteSelect
from core.widgets import ListOrOtherField, SuggestInput

from .models import Client, ClientContact, Order, Project, Sample
from .vocabulary import (
    CONTACT_ROLES,
    CONTAINER_CONDITIONS,
    CONTAINER_TYPES,
    PRESERVATION_CHECKS,
    PRESERVATIVES,
    SEAL_CONDITIONS,
)
from .widgets import RequestedTestsField

DATETIME = {"type": "datetime-local"}
REASON_HELP = "Kept with the change in the record's history."


class StyledForm:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                css = "form-check-input"
            else:
                css = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs.setdefault("class", css)


class GroupedForm:
    """Sections for receipt/form.html: ((legend, ((field name, column class), ...)), ...).

    A field the form does not have is skipped, so create and edit forms share one layout."""

    groups = ()

    def grouped(self):
        out = []
        for legend, layout in self.groups:
            fields = [(self[name], css) for name, css in layout if name in self.fields]
            if fields:
                out.append((legend, fields))
        return out


class StaffChoiceField(forms.ModelChoiceField):
    """Active laboratory accounts; a client portal account is never offered."""

    def __init__(self, **kwargs):
        kwargs.setdefault(
            "queryset",
            get_user_model()
            .objects.filter(is_active=True, client__isnull=True)
            .order_by("first_name", "last_name", "username"),
        )
        kwargs.setdefault("empty_label", "Not assigned")
        kwargs.setdefault("widget", RemoteSelect("staff"))
        super().__init__(**kwargs)

    def label_from_instance(self, user):
        return user.get_full_name() or user.get_username()


def instrument_suggestions():
    """In-service thermometers from the equipment register, then instruments typed before."""
    from laboratory.models import Equipment  # laboratory imports receipt.models

    equipment = Equipment.objects.filter(
        status=Equipment.Status.IN_SERVICE, equipment_type__icontains="thermometer"
    ).order_by("asset_id")
    used = (
        Sample.objects.exclude(temperature_instrument="")
        .order_by("-received_at")
        .values_list("temperature_instrument", flat=True)[:50]
    )
    return [*(f"{item.asset_id} {item.name}" for item in equipment), *used][:30]


def location_suggestions():
    """Storage locations already in use, so one place is spelled one way."""
    return list(
        Sample.objects.exclude(current_location="")
        .order_by("current_location")
        .values_list("current_location", flat=True)
        .distinct()[:40]
    )


class ClientForm(GroupedForm, StyledForm, forms.ModelForm):
    groups = (
        ("Account", (("name", "col-md-8"), ("account_number", "col-md-4"))),
        (
            "Primary contact",
            (("contact_name", "col-md-5"), ("email", "col-md-4"), ("phone", "col-md-3")),
        ),
        (
            "Mailing address",
            (
                ("address_line1", "col-md-7"),
                ("address_line2", "col-md-5"),
                ("city", "col-md-5"),
                ("state", "col-md-4"),
                ("postal_code", "col-md-3"),
            ),
        ),
        (
            "Invoicing",
            (
                ("billing_contact_name", "col-md-6"),
                ("billing_email", "col-md-6"),
                ("billing_address", "col-12"),
                ("payment_terms", "col-md-4"),
                ("po_required", "col-md-8 align-self-end"),
            ),
        ),
        ("Reports and notes", (("report_preferences", "col-12"), ("notes", "col-12"))),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = Client
        fields = [
            "name",
            "account_number",
            "contact_name",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "billing_contact_name",
            "billing_email",
            "billing_address",
            "payment_terms",
            "po_required",
            "report_preferences",
            "notes",
        ]
        labels = {
            "contact_name": "Contact name",
            "report_preferences": "Report instructions",
            "notes": "Internal notes",
        }
        help_texts = {
            "name": "The organization as it should appear on reports.",
            "contact_name": "The person reports are addressed to.",
            "billing_contact_name": "Leave the invoicing fields blank when the primary contact "
            "also receives invoices.",
            "report_preferences": "How this client wants reports: recipients, format, "
            "electronic deliverable. Information only; nothing is sent automatically.",
        }
        widgets = {
            "billing_address": forms.Textarea(attrs={"rows": 3}),
            "report_preferences": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "phone": forms.TextInput(attrs={"type": "tel", "autocomplete": "off"}),
            "postal_code": forms.TextInput(attrs={"inputmode": "numeric"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["state"].choices = [("", "Select…"), *self.fields["state"].choices[1:]]
        self.fields["payment_terms"].choices = [
            ("", "Not set"),
            *self.fields["payment_terms"].choices[1:],
        ]


class ProjectForm(GroupedForm, StyledForm, forms.ModelForm):
    project_manager = StaffChoiceField(required=False, label="Laboratory project manager")
    groups = (
        (
            "Project",
            (
                ("client", "col-12"),
                ("name", "col-md-8"),
                ("number", "col-md-4"),
                ("project_manager", "col-md-6"),
                ("turnaround_days", "col-md-6"),
            ),
        ),
        (
            "Program and site",
            (
                ("regulatory_program", "col-md-4"),
                ("program", "col-md-4"),
                ("site_state", "col-md-4"),
                ("site_address", "col-12"),
            ),
        ),
        ("Intake", (("po_number", "col-md-4"), ("instructions", "col-md-8"))),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "client" in self.fields:
            self.fields["client"].queryset = Client.objects.filter(is_active=True)
            self.fields["client"].widget = RemoteSelect("active-clients")
            self.fields["client"].widget.choices = self.fields["client"].choices
        if self.instance.project_manager_id:
            # Keep the recorded manager selectable even if that account is now inactive.
            field = self.fields["project_manager"]
            field.queryset = field.queryset | get_user_model().objects.filter(
                pk=self.instance.project_manager_id
            )
        self.fields["regulatory_program"].choices = [
            ("", "Not recorded"),
            *self.fields["regulatory_program"].choices[1:],
        ]
        self.fields["site_state"].choices = [
            ("", "Not recorded"),
            *self.fields["site_state"].choices[1:],
        ]

    class Meta:
        model = Project
        fields = [
            "client",
            "name",
            "number",
            "project_manager",
            "turnaround_days",
            "regulatory_program",
            "program",
            "site_state",
            "site_address",
            "po_number",
            "instructions",
        ]
        labels = {
            "name": "Project name",
            "turnaround_days": "Standard turnaround",
        }
        help_texts = {
            "program": "Permit number or program label, printed on reports.",
            "turnaround_days": "The agreed standard. It does not set a delivery target.",
        }
        widgets = {
            "instructions": forms.Textarea(attrs={"rows": 3}),
            "turnaround_days": forms.NumberInput(attrs={"unit": "days"}),
        }


SAMPLE_TESTS = (
    TestDefinition.objects.filter(is_active=True, method__is_active=True)
    .select_related("method", "analyte")
    .order_by("analyte__name", "method__code", "method__version", "matrix", "pk")
)


class ReceiptForm(StyledForm, forms.ModelForm):
    collection_location = forms.CharField(
        max_length=200, help_text="Specific place where this sample was collected."
    )
    project = forms.ModelChoiceField(
        queryset=Project.objects.filter(is_active=True, client__is_active=True).select_related(
            "client"
        ),
        widget=RemoteSelect("active-projects"),
    )
    coc_reference = forms.CharField(max_length=120, label="Chain of custody reference")
    source_type = forms.ChoiceField(
        required=False,
        label="Sample source",
        choices=[("", "Not recorded"), *Sample.source_choices()],
        help_text="What was sampled. It must belong to the matrix.",
    )
    seal_condition = ListOrOtherField(SEAL_CONDITIONS, max_length=160, label="Custody seal")
    preservation_check = ListOrOtherField(PRESERVATION_CHECKS, max_length=200)
    tests = RequestedTestsField(
        label="Tests",
        help_text=(
            "Choose tests for the sample matrix. Changing matrix keeps your selections for review."
        ),
        queryset=SAMPLE_TESTS,
    )
    container_description = ListOrOtherField(
        CONTAINER_TYPES, max_length=160, label="Container type"
    )
    container_condition = ListOrOtherField(
        CONTAINER_CONDITIONS, max_length=160, label="Container condition"
    )
    relinquished_by = forms.CharField(
        max_length=160, help_text="Who handed the samples over, as signed on the chain of custody."
    )

    class Meta:
        model = Sample
        fields = [
            "project",
            "coc_reference",
            "client_sample_id",
            "sample_type",
            "matrix",
            "source_type",
            "collection_type",
            "collection_started_at",
            "collected_at",
            "collector_name",
            "collection_location",
            "remarks",
            "received_at",
            "cooler_temperature",
            "temperature_instrument",
            "coolant",
            "seal_condition",
            "preservation_check",
            "discrepancy",
            "disposition",
            "disposition_note",
            "current_location",
            "tests",
            "container_description",
            "container_condition",
            "relinquished_by",
        ]
        widgets = {
            **{
                name: forms.DateTimeInput(attrs=DATETIME, format="%Y-%m-%dT%H:%M")
                for name in ["collection_started_at", "collected_at", "received_at"]
            },
            "remarks": forms.Textarea(attrs={"rows": 2}),
            "temperature_instrument": SuggestInput(suggestions=instrument_suggestions),
            "current_location": SuggestInput(suggestions=location_suggestions),
            "cooler_temperature": forms.NumberInput(attrs={"unit": "°C"}),
        }
        labels = {
            "client_sample_id": "Client sample ID",
            "sample_type": "Sample type",
            "collection_started_at": "Composite started at",
            "cooler_temperature": "Cooler temperature",
            "collected_at": "Collected / composite ended at",
            "temperature_instrument": "Thermometer",
            "current_location": "Storage location",
        }
        help_texts = {
            "received_at": "Times use the configured laboratory time zone.",
            "sample_type": "Field designation from the chain of custody.",
            "collection_started_at": "Composites only.",
            "cooler_temperature": "As read; it is never rounded.",
            "temperature_instrument": "Thermometer or IR gun used for the reading.",
            "current_location": "Where the sample is stored now.",
            "discrepancy": "Anything that differs from the chain of custody or labels.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A coolant is recorded at every new receipt; older samples may have none on file.
        self.fields["coolant"].required = True
        self.fields["coolant"].choices = [("", "Select…"), *Sample.Coolant.choices]
        self.fields["sample_type"].required = False

    def clean(self):
        data = super().clean()
        if "sample_type" in data:  # omitted means an ordinary field sample
            data["sample_type"] = data["sample_type"] or Sample.SampleType.SAMPLE
        if "cooler_temperature" in data:
            data["cooler_temperature_original"] = self.data.get("cooler_temperature", "").strip()
        matrix = data.get("matrix")
        if matrix and not Sample.source_matches(matrix, data.get("source_type", "")):
            self.add_error(
                "source_type",
                f"This source does not belong to {Sample.Matrix(matrix).label}. "
                "Choose another source or correct the matrix.",
            )
        if matrix and data.get("tests"):
            incompatible = [test for test in data["tests"] if test.matrix != matrix]
            if incompatible:
                labels = "; ".join(
                    self.fields["tests"].label_from_instance(test) for test in incompatible
                )
                self.add_error(
                    "tests",
                    f"These tests do not match {Sample.Matrix(matrix).label}: {labels}. "
                    "Remove them or correct the sample matrix. Your selections were kept.",
                )
        return data


class TransferForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    to_location = forms.CharField(
        max_length=160, widget=SuggestInput(suggestions=location_suggestions)
    )
    relinquished_by = forms.CharField(max_length=160)
    received_by = forms.CharField(max_length=160)
    occurred_at = forms.DateTimeField(
        initial=timezone.now,
        widget=forms.DateTimeInput(attrs=DATETIME, format="%Y-%m-%dT%H:%M"),
    )
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


class ClientEditForm(ClientForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )

    class Meta(ClientForm.Meta):
        fields = [*ClientForm.Meta.fields, "is_active"]
        labels = {**ClientForm.Meta.labels, "is_active": "Active (accepts new intake)"}


class ProjectEditForm(ProjectForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )

    class Meta(ProjectForm.Meta):
        fields = [name for name in ProjectForm.Meta.fields if name != "client"] + ["is_active"]
        labels = {**ProjectForm.Meta.labels, "is_active": "Active (accepts new intake)"}


ORDER_DETAIL_LAYOUT = (
    (
        "Turnaround and billing",
        (("priority", "col-md-3"), ("due_at", "col-md-5"), ("po_number", "col-md-4")),
    ),
    (
        "Shipment",
        (
            ("delivery_method", "col-md-4"),
            ("carrier_tracking", "col-md-5"),
            ("cooler_count", "col-md-3"),
        ),
    ),
    ("Instructions", (("instructions", "col-12"),)),
)
ORDER_DETAIL_HELP = {
    "po_number": "Left blank on a new chain of custody, the project's standing PO is used.",
    "delivery_method": "How the samples reached, or will reach, the laboratory.",
    "cooler_count": "Coolers or packages in this shipment.",
}


class OrderForm(GroupedForm, StyledForm, forms.ModelForm):
    groups = (
        ("Chain of custody", (("project", "col-md-7"), ("coc_reference", "col-md-5"))),
        *ORDER_DETAIL_LAYOUT,
    )

    class Meta:
        model = Order
        fields = [
            "project",
            "coc_reference",
            "priority",
            "due_at",
            "po_number",
            "delivery_method",
            "carrier_tracking",
            "cooler_count",
            "instructions",
        ]
        labels = {"coc_reference": "Chain of custody reference", "due_at": "Delivery target"}
        help_texts = ORDER_DETAIL_HELP
        widgets = {
            "due_at": forms.DateTimeInput(attrs=DATETIME, format="%Y-%m-%dT%H:%M"),
            "instructions": forms.Textarea(attrs={"rows": 3}),
            "cooler_count": forms.NumberInput(attrs={"min": 1, "max": 200}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.filter(
            is_active=True, client__is_active=True
        ).select_related("client")
        self.fields["project"].widget = RemoteSelect("active-projects")
        self.fields["project"].widget.choices = self.fields["project"].choices
        self.fields["delivery_method"].choices = [
            ("", "Not recorded"),
            *Order.DeliveryMethod.choices,
        ]
        self.fields["priority"].required = False

    def clean_priority(self):  # omitted means standard turnaround
        return self.cleaned_data["priority"] or Order.Priority.STANDARD


class OrderDetailsForm(GroupedForm, StyledForm, forms.ModelForm):
    """Purchase order, turnaround, shipment and instructions of an existing chain of custody.
    Its reference and delivery target change through their own reasoned corrections."""

    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )
    groups = (*ORDER_DETAIL_LAYOUT, ("Change record", (("reason", "col-12"),)))

    class Meta:
        model = Order
        fields = [
            "priority",
            "po_number",
            "delivery_method",
            "carrier_tracking",
            "cooler_count",
            "instructions",
        ]
        help_texts = {k: v for k, v in ORDER_DETAIL_HELP.items() if k != "po_number"}
        widgets = {
            "instructions": forms.Textarea(attrs={"rows": 3}),
            "cooler_count": forms.NumberInput(attrs={"min": 1, "max": 200}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["expected_revision"] = self.instance.revision
        self.fields["delivery_method"].choices = [
            ("", "Not recorded"),
            *Order.DeliveryMethod.choices,
        ]
        self.fields["priority"].required = False
        self.recorded_priority = self.instance.priority

    def clean_priority(self):
        return self.cleaned_data["priority"] or self.recorded_priority


class OrderSampleForm(ReceiptForm):
    project = None
    coc_reference = None
    container_description = None
    container_condition = None
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)

    class Meta(ReceiptForm.Meta):
        fields = [
            name
            for name in ReceiptForm.Meta.fields
            if name
            not in {"project", "coc_reference", "container_description", "container_condition"}
        ]


class ContainerForm(StyledForm, forms.Form):
    description = ListOrOtherField(CONTAINER_TYPES, max_length=160, label="Container type")
    preservation = ListOrOtherField(
        PRESERVATIVES, max_length=200, required=False, label="Preservative"
    )
    condition = ListOrOtherField(CONTAINER_CONDITIONS, max_length=160, label="Condition at receipt")


class BaseContainerFormSet(forms.BaseFormSet):
    default_error_messages = {"too_few_forms": "Add at least one container."}


ContainerFormSet = forms.formset_factory(
    ContainerForm,
    formset=BaseContainerFormSet,
    extra=2,
    min_num=1,
    validate_min=True,
    max_num=20,
    validate_max=True,
    absolute_max=20,
    can_delete=True,
)


class DispositionForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    disposition = forms.ChoiceField(choices=Sample.Disposition.choices)
    reason = forms.CharField(
        label="Client response / reason", widget=forms.Textarea(attrs={"rows": 4})
    )


class ContactForm(StyledForm, forms.ModelForm):
    role = ListOrOtherField(
        CONTACT_ROLES,
        max_length=80,
        required=False,
        help_text="What the laboratory contacts this person about.",
    )

    class Meta:
        model = ClientContact
        fields = ["name", "role", "email", "phone", "receives_reports"]


class ContactEditForm(ContactForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))

    class Meta(ContactForm.Meta):
        fields = [*ContactForm.Meta.fields, "is_active"]


class OrderActionForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


class LabelOptionsForm(forms.Form):
    """Choices of the label download menu (receipt/labels_menu.html); sample_labels checks them."""

    kind = forms.ChoiceField(
        label="Print labels for",
        choices=[("containers", "Each container"), ("samples", "Each sample")],
        initial="containers",
        required=False,
        widget=forms.RadioSelect,
    )
    copies = forms.IntegerField(
        label="Copies of each",
        min_value=1,
        max_value=10,
        initial=1,
        required=False,
        help_text="PDF of 4 × 2 in labels, one per page. Print at actual size. "
        "Up to 200 labels per download.",
        widget=forms.NumberInput(attrs={"step": 1, "inputmode": "numeric"}),
    )
