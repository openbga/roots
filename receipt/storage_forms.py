from django import forms
from django.utils import timezone

from core.widgets import ListOrOtherField

from .filters import SearchFilter
from .forms import StyledForm
from .vocabulary import DISPOSAL_METHODS


class RetentionForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    retention_until = forms.DateField(
        label="Retain physical sample through",
        help_text=(
            "Laboratory-selected date, inclusive. This does not set electronic record retention."
        ),
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    reason = forms.CharField(
        label="Retention basis / reason", widget=forms.Textarea(attrs={"rows": 3})
    )


class DisposalHoldForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    disposal_hold = forms.TypedChoiceField(
        label="Disposal hold",
        choices=[("true", "Place hold"), ("false", "Remove hold")],
        coerce=lambda value: value == "true",
    )
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


class DisposalForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    occurred_at = forms.DateTimeField(
        label="Physically disposed at",
        initial=timezone.now,
        help_text="Laboratory time zone. Record a completed physical action.",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    method = ListOrOtherField(DISPOSAL_METHODS, max_length=160, label="Disposal method")
    destination = forms.CharField(max_length=160, label="Disposal destination")
    relinquished_by = forms.CharField(max_length=160)
    received_by = forms.CharField(max_length=160)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))
    confirm = forms.BooleanField(
        label="I confirm this records physical disposal and the laboratory authorized the action."
    )


class StorageFilter(SearchFilter):
    state = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All samples"),
            ("stored", "In storage"),
            ("unconfigured", "Retention date not set"),
            ("retained", "Within retention date"),
            ("elapsed", "Retention date elapsed"),
            ("held", "Disposal blocked by hold"),
            ("disposed", "Physically disposed"),
        ],
    )
