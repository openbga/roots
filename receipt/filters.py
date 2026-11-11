"""Validated list filters. Inactive records remain searchable historical records."""

from django import forms

from .forms import StaffChoiceField, StyledForm
from .models import Client, Order, Project, Sample
from .vocabulary import US_STATES


class SearchFilter(StyledForm, forms.Form):
    q = forms.CharField(required=False, max_length=200, label="Search")

    def clean(self):
        data = super().clean()
        client, project = data.get("client"), data.get("project")
        if client and project and project.client_id != client.pk:
            self.add_error("project", "Select a project belonging to the selected client.")
        for prefix in ("received", "collected", "created"):
            start, end = data.get(f"{prefix}_from"), data.get(f"{prefix}_to")
            if start and end and start > end:
                self.add_error(f"{prefix}_to", "End date must be on or after the start date.")
        return data


class ClientFilter(SearchFilter):
    active = forms.ChoiceField(
        required=False,
        choices=[("", "All statuses"), ("all", "All"), ("yes", "Active"), ("no", "Inactive")],
        label="Status",
    )
    state = forms.ChoiceField(required=False, choices=[("", "Any state"), *US_STATES])


class ProjectFilter(ClientFilter):
    state = None
    client = forms.ModelChoiceField(
        queryset=Client.objects.all(), required=False, empty_label="Any client"
    )
    regulatory_program = forms.ChoiceField(
        required=False,
        label="Program",
        choices=[("", "Any program"), *Project.RegulatoryProgram.choices],
    )
    project_manager = StaffChoiceField(
        required=False, label="Project manager", empty_label="Any project manager"
    )


class OrderFilter(SearchFilter):
    client = forms.ModelChoiceField(
        queryset=Client.objects.all(), required=False, empty_label="Any client"
    )
    project = forms.ModelChoiceField(
        queryset=Project.objects.select_related("client"), required=False, empty_label="Any project"
    )
    priority = forms.ChoiceField(
        required=False,
        label="Turnaround",
        choices=[("", "Any turnaround"), *Order.Priority.choices],
    )
    created_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    created_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))


class SampleFilter(SearchFilter):
    client = forms.ModelChoiceField(
        queryset=Client.objects.all(), required=False, empty_label="Any client"
    )
    project = forms.ModelChoiceField(
        queryset=Project.objects.select_related("client"), required=False, empty_label="Any project"
    )
    matrix = forms.ChoiceField(
        required=False, choices=[("", "All matrices"), *Sample.Matrix.choices]
    )
    disposition = forms.ChoiceField(
        required=False, choices=[("", "All dispositions"), *Sample.Disposition.choices]
    )
    sample_type = forms.ChoiceField(
        required=False, choices=[("", "All sample types"), *Sample.SampleType.choices]
    )
    source_type = forms.ChoiceField(
        required=False,
        label="Sample source",
        choices=[("", "All sources"), *Sample.Source.choices],
    )
    received_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    received_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    collected_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    collected_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
