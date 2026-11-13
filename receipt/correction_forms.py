from django import forms

from catalog.models import TestDefinition
from core.widgets import ListOrOtherField, SuggestInput

from .corrections import unremovable_tests
from .forms import StyledForm, instrument_suggestions
from .models import RequestedTest, Sample
from .vocabulary import PRESERVATION_CHECKS, SEAL_CONDITIONS
from .widgets import RequestedTestsField, test_option_data


class ReceiptCorrectionForm(StyledForm, forms.ModelForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(max_length=4000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["expected_revision"] = self.instance.revision


class IdentityCorrectionForm(ReceiptCorrectionForm):
    collection_location = forms.CharField(
        max_length=200, label="Collection location", required=False
    )
    source_type = forms.ChoiceField(
        required=False,
        label="Sample source",
        choices=[("", "Not recorded"), *Sample.source_choices()],
    )

    class Meta:
        model = Sample
        fields = [
            "client_sample_id",
            "sample_type",
            "source_type",
            "collector_name",
            "collection_location",
            "remarks",
        ]
        labels = {
            "client_sample_id": "Client sample ID",
            "sample_type": "Sample type",
        }
        widgets = {"remarks": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        matrix = self.instance.get_matrix_display()
        self.fields["source_type"].help_text = f"Must belong to this sample's matrix ({matrix})."
        self.fields["sample_type"].required = False
        self.recorded_type = self.instance.sample_type

    def clean_sample_type(self):
        return self.cleaned_data["sample_type"] or self.recorded_type

    def clean_source_type(self):
        value = self.cleaned_data["source_type"]
        if not Sample.source_matches(self.instance.matrix, value):
            raise forms.ValidationError(
                f"This source does not belong to {self.instance.get_matrix_display()}."
            )
        return value


class CollectionCorrectionForm(ReceiptCorrectionForm):
    class Meta:
        model = Sample
        fields = ["collection_type", "collection_started_at", "collected_at"]
        labels = {
            "collection_started_at": "Composite collection started at",
            "collected_at": "Collected at / composite collection ended at",
        }
        widgets = {
            name: forms.DateTimeInput(
                format="%Y-%m-%dT%H:%M:%S", attrs={"type": "datetime-local", "step": "1"}
            )
            for name in ("collection_started_at", "collected_at")
        }

    def clean(self):
        data = super().clean()
        # A seconds-resolution picker must not silently round an unchanged stored timestamp.
        for name in ("collection_started_at", "collected_at"):
            original = getattr(self.instance, name)
            if original is not None and data.get(name) == original.replace(microsecond=0):
                data[name] = original
        return data


class ObservationsCorrectionForm(ReceiptCorrectionForm):
    seal_condition = ListOrOtherField(SEAL_CONDITIONS, max_length=160, label="Custody seal")
    preservation_check = ListOrOtherField(PRESERVATION_CHECKS, max_length=200)

    class Meta:
        model = Sample
        fields = [
            "cooler_temperature",
            "temperature_instrument",
            "coolant",
            "seal_condition",
            "preservation_check",
            "discrepancy",
        ]
        labels = {
            "cooler_temperature": "Cooler temperature, as read",
            "temperature_instrument": "Thermometer",
        }
        widgets = {
            "cooler_temperature": forms.NumberInput(attrs={"unit": "°C"}),
            "discrepancy": forms.Textarea(attrs={"rows": 3}),
            "temperature_instrument": SuggestInput(suggestions=instrument_suggestions),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["coolant"].choices = [("", "Not recorded"), *Sample.Coolant.choices]

    def clean(self):
        data = super().clean()
        if "cooler_temperature" in data:
            data["cooler_temperature_original"] = self.data.get("cooler_temperature", "").strip()
        return data


class MatrixCorrectionForm(StyledForm, forms.Form):
    """Replaces the matrix and requested tests together; the tests must fit the new matrix."""

    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    matrix = forms.ChoiceField(choices=Sample.Matrix.choices)
    tests = RequestedTestsField(
        label="Tests for the corrected matrix",
        queryset=TestDefinition.objects.filter(is_active=True, method__is_active=True)
        .select_related("method", "analyte")
        .order_by("analyte__name", "method__code", "method__version", "matrix", "pk"),
        help_text="The current tests belong to the recorded matrix and are replaced.",
    )
    reason = forms.CharField(max_length=4000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, sample, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.update({"expected_revision": sample.revision, "matrix": sample.matrix})

    def clean(self):
        data = super().clean()
        matrix, tests = data.get("matrix"), data.get("tests")
        if matrix and tests:
            wrong = [t for t in tests if t.matrix != matrix]
            if wrong:
                labels = "; ".join(self.fields["tests"].label_from_instance(t) for t in wrong)
                self.add_error(
                    "tests",
                    f"These tests do not match {Sample.Matrix(matrix).label}: {labels}. "
                    "Remove them or choose another matrix. Your selections were kept.",
                )
        return data


class TestsChangeForm(StyledForm, forms.Form):
    """Add tests of the sample's matrix, or withdraw tests that have no work yet."""

    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    add = RequestedTestsField(
        label="Add tests",
        required=False,
        queryset=TestDefinition.objects.none(),
        help_text="Active tests for this sample's matrix. Holding times run from collection.",
    )
    remove = forms.ModelMultipleChoiceField(
        label="Withdraw tests",
        required=False,
        queryset=RequestedTest.objects.none(),
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        help_text="Only tests with no analytical or external work can be withdrawn.",
    )
    reason = forms.CharField(max_length=4000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, sample, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["expected_revision"] = sample.revision
        requested = RequestedTest.objects.filter(sample=sample).select_related(
            "definition__analyte", "definition__method"
        )
        self.locked = unremovable_tests(sample)
        self.fields["remove"].queryset = requested.exclude(pk__in=self.locked)
        self.fields["remove"].label_from_instance = lambda r: test_option_data(r.definition)["text"]
        self.fields["add"].queryset = (
            TestDefinition.objects.filter(
                is_active=True, method__is_active=True, matrix=sample.matrix
            )
            .exclude(pk__in=requested.values("definition_id"))
            .select_related("method", "analyte")
            .order_by("analyte__name", "method__code", "method__version", "pk")
        )
        self.fields["remove"].empty_label = None
        self.requested = list(requested.order_by("pk"))

    def clean(self):
        data = super().clean()
        if not data.get("add") and not data.get("remove") and not self.errors:
            raise forms.ValidationError("Choose at least one test to add or withdraw.")
        return data


class OrderReferenceForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    coc_reference = forms.CharField(max_length=120, label="Chain of custody reference")
    reason = forms.CharField(max_length=4000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, order, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.update(
            {"expected_revision": order.revision, "coc_reference": order.coc_reference}
        )
