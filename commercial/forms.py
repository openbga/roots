"""Forms for pricing, quotes and submissions. Services re-check everything the forms check."""

import uuid
from decimal import Decimal

from django import forms
from django.utils import timezone

from catalog.models import TestDefinition
from core.widgets import ListOrOtherField
from receipt.forms import StyledForm
from receipt.models import Client, Project, Sample
from receipt.vocabulary import CONTAINER_TYPES, PRESERVATIVES
from receipt.widgets import RequestedTestsField

from .models import PriceList, Quote, QuoteLine, Submission, SubmissionSample
from .pricing import offered_tests, test_label
from .quotes import MAX_QUANTITY

MONEY_INPUT = {"inputmode": "decimal", "step": "0.01", "min": "0"}
DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
DATETIME = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")


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


# ------------------------------------------------------------------ price lists


class PriceListFilter(FilterForm):
    active = forms.ChoiceField(
        required=False,
        choices=[("", "All"), ("yes", "Active"), ("no", "Inactive")],
        label="Status",
    )


class PriceListForm(GroupedForm, StyledForm, forms.Form):
    """List details plus one optional price per catalog test (blank means not offered)."""

    expected_revision = forms.IntegerField(widget=forms.HiddenInput, required=False)
    name = forms.CharField(max_length=120)
    description = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), help_text="Shown to staff only."
    )
    is_active = forms.BooleanField(required=False, initial=True, label="Active (can be quoted)")
    reason = forms.CharField(
        required=False,
        label="Reason for change",
        help_text="Required when editing. Kept in the audit history.",
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    groups = (("List", ["name", "description", "is_active", "reason"]),)

    def __init__(self, *args, price_list=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.price_list = price_list
        current = {}
        if price_list:
            current = {i.test_id: i.unit_price for i in price_list.items.all()}
            self.initial.update(
                name=price_list.name,
                description=price_list.description,
                is_active=price_list.is_active,
                expected_revision=price_list.revision,
            )
        else:
            del self.fields["reason"]
        tests = {t.pk: t for t in offered_tests()}
        if current:
            tests.update(
                {
                    t.pk: t
                    for t in TestDefinition.objects.filter(pk__in=current).select_related(
                        "method", "analyte"
                    )
                }
            )
        self.test_fields = []
        for test in sorted(
            tests.values(),
            key=lambda t: (t.matrix, t.analyte.name, t.method.code, t.method.version),
        ):
            name = f"price_{test.pk}"
            self.fields[name] = forms.DecimalField(
                required=False,
                min_value=Decimal("0"),
                max_digits=12,
                decimal_places=2,
                label=f"Price for {test_label(test)}",
                widget=forms.NumberInput(
                    attrs={**MONEY_INPUT, "class": "form-control text-end", "placeholder": "—"}
                ),
                initial=current.get(test.pk),
            )
            self.test_fields.append((test, self[name]))

    def clean(self):
        data = super().clean()
        if self.price_list and not (data.get("reason") or "").strip():
            self.add_error("reason", "Give the reason for this change.")
        return data

    def prices(self):
        return {test.pk: self.cleaned_data.get(f"price_{test.pk}") for test, _ in self.test_fields}


# ------------------------------------------------------------------ quotes


class QuoteFilter(FilterForm):
    status = forms.ChoiceField(
        required=False,
        choices=[("", "All statuses"), ("expired", "Expired"), *Quote.Status.choices],
    )
    client = forms.ModelChoiceField(Client.objects.all(), required=False, empty_label="Any client")


class PortalQuoteFilter(FilterForm):
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All statuses"),
            ("issued", "Awaiting your decision"),
            ("accepted", "Accepted"),
            ("declined", "Declined"),
            ("expired", "Expired"),
        ],
    )


class QuoteCreateForm(StyledForm, forms.Form):
    token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    client = forms.ModelChoiceField(Client.objects.filter(is_active=True))
    project = forms.ModelChoiceField(
        Project.objects.filter(is_active=True).select_related("client"),
        required=False,
        help_text="Optional. Choose a project of the selected client.",
    )
    title = forms.CharField(
        max_length=160, help_text="For example: 2026 quarterly outfall monitoring."
    )

    def clean(self):
        data = super().clean()
        client, project = data.get("client"), data.get("project")
        if client and project and project.client_id != client.pk:
            self.add_error("project", "Choose a project belonging to the selected client.")
        return data


class QuoteDraftForm(GroupedForm, StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    title = forms.CharField(max_length=160)
    project = forms.ModelChoiceField(Project.objects.none(), required=False)
    price_list = forms.ModelChoiceField(
        PriceList.objects.none(),
        required=False,
        help_text="Supplies the default price when a test line has no price of its own.",
    )
    valid_until = forms.DateField(
        required=False,
        widget=DATE,
        help_text="Required to issue. Prices are honoured through this date.",
    )
    turnaround_days = forms.IntegerField(
        required=False, min_value=1, max_value=365, label="Turnaround (business days)"
    )
    scope_notes = forms.CharField(
        required=False,
        label="Scope and assumptions",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Shown to the client: sample counts, matrices, holding-time assumptions.",
    )
    terms = forms.CharField(
        required=False,
        label="Terms",
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Shown to the client and frozen when issued.",
    )
    groups = (
        ("Quote", ["title", "project", "price_list"]),
        ("Validity and turnaround", ["valid_until", "turnaround_days"]),
        ("Scope and terms", ["scope_notes", "terms"]),
    )

    def __init__(self, *args, quote, version, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.filter(
            client_id=quote.client_id, is_active=True
        )
        lists = PriceList.objects.filter(is_active=True)
        if version.price_list_id:
            lists = PriceList.objects.filter(is_active=True) | PriceList.objects.filter(
                pk=version.price_list_id
            )
        self.fields["price_list"].queryset = lists.distinct()
        self.initial.update(
            expected_revision=quote.revision,
            title=quote.title,
            project=quote.project_id,
            price_list=version.price_list_id,
            valid_until=version.valid_until,
            turnaround_days=version.turnaround_days,
            scope_notes=version.scope_notes,
            terms=version.terms,
        )


class QuoteLineForm(StyledForm, forms.Form):
    kind = forms.ChoiceField(choices=QuoteLine.Kind.choices, initial=QuoteLine.Kind.TEST)
    test = forms.ModelChoiceField(
        offered_tests().order_by("analyte__name", "method__code", "matrix"),
        required=False,
        empty_label="—",
    )
    description = forms.CharField(
        max_length=240, required=False, help_text="Required for fees and adjustments."
    )
    quantity = forms.IntegerField(
        min_value=1,
        max_value=MAX_QUANTITY,
        initial=1,
        widget=forms.NumberInput(attrs={"min": 1}),
    )
    unit_price = forms.DecimalField(
        required=False,
        max_digits=12,
        decimal_places=2,
        label="Unit price (USD)",
        widget=forms.NumberInput(attrs={"inputmode": "decimal", "step": "0.01"}),
        help_text="Blank uses the price list. Enter discounts as negative amounts.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["test"].label_from_instance = test_label
        self.fields["quantity"].widget.attrs["class"] = "form-control text-end"
        self.fields["unit_price"].widget.attrs["class"] = "form-control text-end"

    def clean(self):
        data = super().clean()
        kind = data.get("kind")
        if kind == QuoteLine.Kind.TEST and not data.get("test"):
            self.add_error("test", "Choose a test for a test line.")
        if kind and kind != QuoteLine.Kind.TEST:
            if data.get("test"):
                self.add_error("test", "Only test lines have a test; clear it or change the type.")
            if not (data.get("description") or "").strip():
                self.add_error("description", "Describe this fee or adjustment.")
            if data.get("unit_price") is None:
                self.add_error("unit_price", "Enter an amount.")
        price = data.get("unit_price")
        if price is not None:
            if kind == QuoteLine.Kind.ADJUSTMENT and price > 0:
                self.add_error("unit_price", "Enter a discount as a negative amount, e.g. -50.00.")
            if kind != QuoteLine.Kind.ADJUSTMENT and price < 0:
                self.add_error("unit_price", "Only a discount may be negative.")
        return data


class BaseLineFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        if not self.lines():
            raise forms.ValidationError("A quote needs at least one line before it can be issued.")

    def lines(self):
        return [
            f.cleaned_data
            for f in self.forms
            if f.cleaned_data and not f.cleaned_data.get("DELETE")
        ]


QuoteLineFormSet = forms.formset_factory(
    QuoteLineForm, formset=BaseLineFormSet, extra=3, can_delete=True, max_num=100, absolute_max=100
)


def line_initial(version):
    return [
        {
            "kind": line.kind,
            "test": line.test_id,
            "description": line.description,
            "quantity": line.quantity,
            "unit_price": line.unit_price,
        }
        for line in version.lines.all()
    ]


class VersionActionForm(forms.Form):
    """Hidden state the actor saw, so a stale or repeated request is detected by the service."""

    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    version_number = forms.IntegerField(widget=forms.HiddenInput)


class ReasonForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


class DeclineForm(StyledForm, forms.Form):
    version_number = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


class AcceptForm(StyledForm, forms.Form):
    version_number = forms.IntegerField(widget=forms.HiddenInput)
    client_reference = forms.CharField(
        required=False,
        max_length=120,
        label="Your purchase order or reference",
        help_text="Optional. Recorded with your acceptance.",
    )
    confirm = forms.BooleanField(
        label="I accept these prices and terms for this laboratory work.",
        error_messages={"required": "Confirm that you accept the prices and terms."},
    )


# ------------------------------------------------------------------ submissions


class SubmissionFilter(FilterForm):
    status = forms.ChoiceField(
        required=False, choices=[("", "All statuses"), *Submission.Status.choices]
    )
    client = forms.ModelChoiceField(Client.objects.all(), required=False, empty_label="Any client")


class PortalSubmissionFilter(FilterForm):
    status = forms.ChoiceField(
        required=False, choices=[("", "All statuses"), *Submission.Status.choices]
    )


class SubmissionForm(GroupedForm, StyledForm, forms.ModelForm):
    token = forms.UUIDField(widget=forms.HiddenInput, required=False)
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, required=False)
    project = forms.ModelChoiceField(Project.objects.none())
    quote = forms.ModelChoiceField(
        Quote.objects.none(),
        required=False,
        label="Accepted quote",
        help_text="Optional. Link the quote these samples are covered by.",
    )

    class Meta:
        model = Submission
        fields = [
            "project",
            "quote",
            "coc_reference",
            "expected_arrival",
            "carrier",
            "cooler_count",
            "contact_name",
            "contact_phone",
            "special_instructions",
        ]
        widgets = {
            "expected_arrival": DATE,
            "special_instructions": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {"cooler_count": "Number of coolers / packages"}
        help_texts = {
            "coc_reference": "The reference printed on your field chain of custody.",
            "contact_name": "Who the laboratory should call about this shipment.",
            "special_instructions": "Rush approval, hazards, permit limits, report instructions.",
        }

    groups = (
        ("Project", ["project", "quote", "coc_reference"]),
        ("Shipment", ["expected_arrival", "carrier", "cooler_count"]),
        ("Contact and notes", ["contact_name", "contact_phone", "special_instructions"]),
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.filter(
            client_id=user.client_id, is_active=True, client__is_active=True
        )
        self.fields["quote"].queryset = Quote.objects.filter(
            client_id=user.client_id, status=Quote.Status.ACCEPTED
        )
        if self.instance.pk:
            self.initial.setdefault("expected_revision", self.instance.revision)
        else:
            self.initial.setdefault("token", uuid.uuid4())

    def service_data(self):
        data = {
            k: v for k, v in self.cleaned_data.items() if k not in {"token", "expected_revision"}
        }
        return data


class SubmissionSampleForm(StyledForm, forms.ModelForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    collection_location = forms.CharField(
        max_length=200, help_text="Specific place, for example Outfall 001 sampling port."
    )
    source_type = forms.ChoiceField(
        required=False,
        label="Sample source",
        choices=[("", "Not stated"), *Sample.source_choices()],
        help_text="What was sampled. It must belong to the matrix.",
    )
    tests = RequestedTestsField(
        label="Intended tests",
        help_text="Choose tests for the sample matrix. The laboratory confirms them at receipt.",
        queryset=TestDefinition.objects.filter(is_active=True, method__is_active=True)
        .select_related("method", "analyte")
        .order_by("analyte__name", "method__code", "method__version", "matrix", "pk"),
    )

    class Meta:
        model = SubmissionSample
        fields = [
            "client_sample_id",
            "sample_type",
            "matrix",
            "source_type",
            "collection_type",
            "collection_started_at",
            "collected_at",
            "planned_collection_on",
            "collector_name",
            "collection_location",
            "field_notes",
            "tests",
        ]
        widgets = {
            "collection_started_at": DATETIME,
            "collected_at": DATETIME,
            "planned_collection_on": DATE,
            "field_notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "client_sample_id": "Your sample ID",
            "sample_type": "Sample type",
            "collected_at": "Collected / composite ended at",
            "planned_collection_on": "Planned collection date",
            "field_notes": "Field notes",
        }
        help_texts = {
            "sample_type": "Mark field blanks, trip blanks and field duplicates.",
            "collection_started_at": "Composites only. Times use the laboratory time zone.",
            "collected_at": (
                "Leave blank if the sample has not been collected yet, for example while "
                "you are preparing bottles. Do not enter an estimate."
            ),
            "planned_collection_on": "Optional. Used only while no collection time is entered.",
            "field_notes": "Field readings or conditions you want the laboratory to know.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sample_type"].required = False

    def clean(self):
        data = super().clean()
        if "sample_type" in data:  # omitted means an ordinary field sample
            data["sample_type"] = data["sample_type"] or Sample.SampleType.SAMPLE
        collected = data.get("collected_at")
        if collected and collected > timezone.now():
            self.add_error("collected_at", "Collection time cannot be in the future.")
        if collected and data.get("collection_type") == "composite":
            if not data.get("collection_started_at"):
                self.add_error("collection_started_at", "Composite samples need a start time.")
        if not collected and data.get("collection_started_at"):
            self.add_error("collected_at", "Enter the collection end time too, or clear the start.")
        if data.get("matrix") and not Sample.source_matches(
            data["matrix"], data.get("source_type", "")
        ):
            self.add_error(
                "source_type",
                f"This source does not belong to {Sample.Matrix(data['matrix']).label}.",
            )
        if data.get("matrix") and data.get("tests"):
            bad = [t for t in data["tests"] if t.matrix != data["matrix"]]
            if bad:
                labels = "; ".join(test_label(t) for t in bad)
                self.add_error(
                    "tests",
                    f"These tests do not match {Sample.Matrix(data['matrix']).label}: {labels}. "
                    "Remove them or correct the matrix. Your selections were kept.",
                )
        return data


class SubmissionContainerForm(StyledForm, forms.Form):
    description = ListOrOtherField(CONTAINER_TYPES, max_length=160, label="Container type")
    preservation = ListOrOtherField(
        PRESERVATIVES,
        max_length=200,
        required=False,
        label="Preservative",
        help_text="What was added to the container in the field.",
    )
    quantity = forms.IntegerField(min_value=1, max_value=20, initial=1)


class BaseSubmissionContainerFormSet(forms.BaseFormSet):
    default_error_messages = {"too_few_forms": "Describe at least one container."}

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        if sum(c["quantity"] for c in self.rows()) > 20:
            raise forms.ValidationError("A sample may have at most 20 containers.")

    def rows(self):
        return [
            {k: v for k, v in f.cleaned_data.items() if k != "DELETE"}
            for f in self.forms
            if f.cleaned_data and not f.cleaned_data.get("DELETE")
        ]


SubmissionContainerFormSet = forms.formset_factory(
    SubmissionContainerForm,
    formset=BaseSubmissionContainerFormSet,
    extra=1,
    min_num=1,
    validate_min=True,
    max_num=20,
    validate_max=True,
    absolute_max=20,
    can_delete=True,
)


class SubmissionActionForm(forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)


class ReviewActionForm(forms.Form):
    snapshot_number = forms.IntegerField(widget=forms.HiddenInput)


class ReturnForm(StyledForm, forms.Form):
    snapshot_number = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="What the client should correct", widget=forms.Textarea(attrs={"rows": 3})
    )
