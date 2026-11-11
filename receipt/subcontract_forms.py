from django import forms
from django.utils import timezone

from .filters import SearchFilter
from .forms import StyledForm
from .models import Container
from .subcontract_models import SubcontractResult


class ContainerChoice(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.label} · {obj.description} · {obj.condition}"


class DispatchForm(StyledForm, forms.Form):
    expected_sample_revision = forms.IntegerField(widget=forms.HiddenInput)
    external_lab = forms.CharField(max_length=160, label="External laboratory")
    external_reference = forms.CharField(
        max_length=160, label="External order / dispatch reference"
    )
    external_sample_id = forms.CharField(
        label="External sample ID",
        max_length=160,
        required=False,
        help_text="If already assigned; otherwise record with the source result.",
    )
    dispatched_at = forms.DateTimeField(
        initial=timezone.now,
        label="Completed handoff at",
        help_text="Laboratory time zone. Record a completed handoff, not a planned shipment.",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    handed_to = forms.CharField(max_length=160, label="Receiving custodian / courier")
    container_ids = ContainerChoice(
        queryset=Container.objects.none(),
        label="Containers handed over",
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        help_text="Choose only the physical containers included in this handoff.",
    )
    reason = forms.CharField(max_length=4000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, sample, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["container_ids"].queryset = sample.containers.order_by("pk")

    def clean_container_ids(self):
        return list(self.cleaned_data["container_ids"].values_list("pk", flat=True))


class ExternalResultForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    external_sample_id = forms.CharField(
        label="External sample ID",
        max_length=160,
        help_text="Actual sample ID assigned by the external laboratory, as shown on the source.",
    )
    observed_text = forms.CharField(
        max_length=60,
        strip=False,
        label="Exact reported result",
        help_text="Enter the number exactly as reported, or ND. Do not enter comparison symbols.",
    )
    reporting_limit = forms.CharField(max_length=60, label="Reported reporting limit")
    dilution = forms.CharField(max_length=60, label="Reported dilution")
    unit = forms.CharField(max_length=40, label="Reported unit")
    external_method = forms.CharField(max_length=200, label="Reported method and version")
    external_analyst = forms.CharField(max_length=160)
    analyzed_at = forms.DateTimeField(
        label="External analysis started at",
        help_text="Laboratory time zone.",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    qualifier_text = forms.CharField(
        required=False,
        max_length=4000,
        label="External qualifiers and meanings",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    external_report_reference = forms.CharField(max_length=160)
    source_pdf = forms.FileField(
        label="External source report PDF",
        help_text=(
            "At most 10 MiB. PDF header and end marker are checked; contents are not validated. "
            "Staff access only. Select the file again if the form reports an error."
        ),
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,application/pdf"}),
    )
    reason = forms.CharField(
        required=False,
        max_length=4000,
        label="Correction reason / source note",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, is_correction=False, known_external_sample_id="", **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].required = is_correction
        self.fields["external_sample_id"].required = not bool(known_external_sample_id)


class SourceReviewForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    decision = forms.ChoiceField(choices=SubcontractResult.Decision.choices)
    note = forms.CharField(max_length=4000, widget=forms.Textarea(attrs={"rows": 4}))


class SubcontractCancelForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(max_length=4000, widget=forms.Textarea(attrs={"rows": 4}))


class SubcontractFilter(SearchFilter):
    state = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All subcontracts"),
            ("awaiting", "Awaiting source result"),
            ("unreviewed", "Awaiting source review"),
            ("accepted", "Accepted source"),
            ("rejected", "Rejected source"),
            ("cancelled", "Cancelled dispatch"),
        ],
    )
