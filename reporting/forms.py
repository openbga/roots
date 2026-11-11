from django import forms
from django.utils import timezone

from receipt.models import Project, Sample

from .models import MAX_NARRATIVE


class SampleChoiceField(forms.ModelMultipleChoiceField):
    widget = forms.CheckboxSelectMultiple

    def label_from_instance(self, obj):
        collected = timezone.localtime(obj.collected_at).strftime("%Y-%m-%d %H:%M")
        return (
            f"{obj.lab_id} · {obj.client_sample_id} · {obj.get_matrix_display()} · "
            f"collected {collected} · {obj.get_disposition_display()}"
        )


class ProjectChoiceForm(forms.Form):
    project = forms.ModelChoiceField(
        Project.objects.select_related("client").order_by("client__name", "name"),
        empty_label="Choose a project",
        help_text="A report covers samples of one client project.",
    )


class ReportForm(forms.Form):
    title = forms.CharField(max_length=160)
    samples = SampleChoiceField(
        Sample.objects.none(), help_text="Samples of this project to include."
    )
    narrative = forms.CharField(
        required=False,
        max_length=MAX_NARRATIVE,
        widget=forms.Textarea(attrs={"rows": 4}),
        label="Case narrative",
        help_text="Printed on the report. Describe anything the client needs to interpret "
        "the results.",
    )

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["samples"].queryset = Sample.objects.filter(order__project=project).order_by(
            "-received_at", "lab_id"
        )


class ReleaseForm(forms.Form):
    fingerprint = forms.CharField(widget=forms.HiddenInput, max_length=64)
    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Amendment reason",
        help_text="Required for an amendment. It is printed on the new version.",
    )
    confirm = forms.BooleanField(
        label="I authorize release of exactly the content previewed above."
    )

    def __init__(self, *args, amendment, **kwargs):
        super().__init__(*args, **kwargs)
        self.amendment = amendment
        if amendment:
            self.fields["reason"].required = True
        else:
            del self.fields["reason"]
