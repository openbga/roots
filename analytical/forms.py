"""Forms collect input as text and hand it to services, which parse strictly and validate.

Widget classes come from the shared form templates; only the dense entry grids ask for the
small control size explicitly.
"""

import base64
import binascii
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q

from catalog.models import Method, TestDefinition
from laboratory.models import Equipment
from receipt.models import Client, Sample

from . import import_profiles as ip
from . import importing
from .models import (
    Batch,
    HoldingTimeRule,
    ImportProfile,
    MDLObservation,
    MDLStudy,
    PreparationRequirement,
    PreservationRule,
    QCCorrectiveAction,
    QCLimit,
    QCRequirement,
    QCType,
    Qualifier,
    ResultBasis,
)
from .parsing import parse_decimal
from .preparation import batch_sample_ids
from .qc import NEEDS_NATIVE, NEEDS_PARENT, NEEDS_REFERENCE, NEEDS_SPIKE, trim
from .selectors import BATCH_NUMBER

MATRICES = [("water", "Water"), ("wastewater", "Wastewater"), ("soil", "Soil")]
MDL_DECISIONS = [
    ("adopted", "Adopt the verified MDL"),
    ("retained", "Keep the existing MDL (II.4(f), when eligible)"),
    ("rejected", "Reject this calculation"),
]
SMALL = "form-control form-control-sm"
LOCAL_INPUT = ["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]


def local_datetime(label, required=True, help_text="Lab local time.", small=False):
    attrs = {"type": "datetime-local"}
    if small:
        attrs["class"] = SMALL
    return forms.DateTimeField(
        label=label,
        required=required,
        input_formats=LOCAL_INPUT,
        help_text=help_text,
        widget=forms.DateTimeInput(attrs=attrs, format="%Y-%m-%dT%H:%M"),
    )


def raw_text(label, required=False, max_length=60, small=False, help_text="", **widget_attrs):
    """Text field that keeps exactly what was typed (no stripping)."""
    attrs = (
        {"class": SMALL, "autocomplete": "off", "spellcheck": "false", **widget_attrs}
        if small
        else {"autocomplete": "off", "spellcheck": "false", **widget_attrs}
    )
    return forms.CharField(
        label=label,
        required=required,
        strip=False,
        max_length=max_length,
        help_text=help_text,
        widget=forms.TextInput(attrs=attrs),
    )


def add_service_errors(form, exc):
    """Show a service ValidationError on the form: per field where it names one."""
    if hasattr(exc, "error_dict"):
        for name, errors in exc.error_dict.items():
            target = name if name in form.fields else None
            for err in errors:
                form.add_error(target, err)
    else:
        for message in exc.messages:
            form.add_error(None, message)


# ------------------------------------------------------------------ worklist, batches


def name_empty_choices(form):
    """Replace Django's blank "---------" option with a label that says what it means."""
    for name, field in form.fields.items():
        label = str(field.label or name.replace("_", " "))
        subject = label[0].lower() + label[1:] if label[1:2].islower() else label
        blank = f"Choose {subject}" if field.required else "Not set"
        if isinstance(field, forms.ModelChoiceField):
            if field.empty_label == "---------":
                field.empty_label = blank
        elif isinstance(field, forms.ChoiceField) and field.choices[:1] == [("", "---------")]:
            field.choices = [("", blank), *field.choices[1:]]


def date_field(label):
    return forms.DateField(
        label=label, required=False, widget=forms.DateInput(attrs={"type": "date"})
    )


class GridFilterForm(forms.Form):
    """Base for GET filter forms. Page size is the shared pagination footer's per_page."""


def _search(placeholder):
    return forms.CharField(
        label="Search", required=False, widget=forms.SearchInput(attrs={"placeholder": placeholder})
    )


def _method_filter():
    return forms.ModelChoiceField(
        queryset=Method.objects.filter(is_active=True).order_by("code", "version"),
        required=False,
        empty_label="Any method",
    )


MATRIX_FILTER = [("", "Any matrix"), *MATRICES]


class WorklistFilterForm(GridFilterForm):
    q = _search("Lab ID, client, analyte")
    method = _method_filter()
    matrix = forms.ChoiceField(choices=MATRIX_FILTER, required=False)
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Not yet approved"),
            ("not_batched", "Not in a batch"),
            ("external", "Sent to an external laboratory"),
            ("awaiting", "In batch, awaiting entry"),
            ("draft", "Entered"),
            ("submitted", "Awaiting review"),
            ("returned", "Returned"),
            ("approved", "Approved"),
            ("all", "All statuses"),
        ],
    )
    holding = forms.ChoiceField(
        label="Holding time",
        required=False,
        choices=[("", "Any"), ("expired", "Expired"), ("at_risk", "At risk or expired")],
    )
    client = forms.ModelChoiceField(
        queryset=Client.objects.order_by("name"), required=False, empty_label="Any client"
    )
    batch = forms.CharField(
        label="In batch",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "B-000123 or 123"}),
    )
    received_from = date_field("Received from")
    received_to = date_field("Received to")

    def clean_batch(self):
        """A batch filter that is not a batch number is an error, never silently dropped."""
        text = (self.cleaned_data.get("batch") or "").strip()
        match = BATCH_NUMBER.match(text) if text else None
        if text and not (match and int(match.group(1)) >= 1):
            raise ValidationError("Enter a batch number such as B-000123 or 123.")
        return text


class BatchFilterForm(GridFilterForm):
    q = _search("Batch, method, sample ID, analyte")
    status = forms.ChoiceField(required=False, choices=[("", "Any status"), *Batch.Status.choices])
    method = _method_filter()
    matrix = forms.ChoiceField(choices=MATRIX_FILTER, required=False)
    attention = forms.ChoiceField(
        label="Review",
        required=False,
        choices=[("", "Any"), ("awaiting", "Has results awaiting review")],
    )
    created_from = date_field("Created from")
    created_to = date_field("Created to")


class ResultsFilterForm(GridFilterForm):
    q = _search("Sample ID, client sample ID, analyte")
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Any status"),
            ("enterable", "Needs entry (awaiting, entered, returned)"),
            ("awaiting", "Awaiting entry"),
            ("draft", "Entered"),
            ("submitted", "Awaiting review"),
            ("returned", "Returned"),
            ("approved", "Approved"),
        ],
    )
    holding = forms.ChoiceField(
        label="Holding time",
        required=False,
        choices=[
            ("", "Any"),
            ("met", "Met"),
            ("exceeded", "Exceeded"),
            ("not_evaluable", "Not evaluable"),
            ("not_evaluated", "No holding rule"),
            ("none", "Nothing entered"),
        ],
    )
    qc = forms.ChoiceField(
        label="QC",
        required=False,
        choices=[("", "Any"), ("held", "Held by QC"), ("clear", "No QC hold")],
    )


class EntryFilterForm(GridFilterForm):
    q = _search("Sample ID or analyte")
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All that need entry"),
            ("awaiting", "Awaiting entry"),
            ("draft", "Entered, not submitted"),
            ("returned", "Returned"),
        ],
    )


class AddTestsFilterForm(GridFilterForm):
    q = _search("Lab ID, client sample ID, analyte")
    kind = forms.ChoiceField(
        label="Kind of work",
        required=False,
        choices=[("", "All"), ("new", "Not yet analysed"), ("reanalysis", "Reanalysis")],
    )
    client = forms.ModelChoiceField(
        queryset=Client.objects.order_by("name"), required=False, empty_label="Any client"
    )


class ConfigFilterForm(GridFilterForm):
    """Method filter of the QC configuration page (it applies to five of the six tables)."""

    method = forms.ModelChoiceField(
        queryset=Method.objects.order_by("code", "version"),
        required=False,
        empty_label="All methods",
    )


class ReviewListFilterForm(GridFilterForm):
    method = _method_filter()
    matrix = forms.ChoiceField(choices=MATRIX_FILTER, required=False)


class ReviewBatchFilterForm(GridFilterForm):
    q = _search("Sample ID or analyte")
    state = forms.ChoiceField(
        label="Show",
        required=False,
        choices=[
            ("", "All awaiting review"),
            ("ready", "Ready to approve"),
            ("held", "Held (QC or holding time)"),
            ("own", "My own work"),
        ],
    )


class BatchForm(forms.Form):
    method = forms.ModelChoiceField(
        queryset=Method.objects.filter(is_active=True).order_by("code", "version")
    )
    matrix = forms.ChoiceField(choices=MATRICES)
    prepared_at = local_datetime(
        "Preparation start",
        required=False,
        help_text="Lab local time. Needed where a holding rule starts or ends at preparation.",
    )
    equipment = forms.ModelChoiceField(
        queryset=Equipment.objects.exclude(status="retired").order_by("asset_id"),
        required=False,
        label="Instrument or equipment",
        empty_label="Not recorded",
        help_text="Primary instrument for the batch, shown beside its results for information. "
        "Approval and release do not depend on it.",
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].empty_label = "Choose a method"
        name_empty_choices(self)


class BatchEquipmentForm(forms.Form):
    equipment = forms.ModelChoiceField(
        queryset=Equipment.objects.none(),
        required=False,
        label="Instrument or equipment",
        empty_label="Not recorded",
    )

    def __init__(self, *args, batch, **kwargs):
        super().__init__(*args, **kwargs)
        keep = Q(pk=batch.equipment_id) if batch.equipment_id else Q(pk__in=[])
        self.fields["equipment"].queryset = Equipment.objects.filter(
            ~Q(status="retired") | keep
        ).order_by("asset_id")
        self.initial.setdefault("equipment", batch.equipment_id)


class PreparationForm(forms.Form):
    prepared_at = local_datetime("Preparation start")


class QCSampleForm(forms.Form):
    qc_type = forms.ChoiceField(label="QC type", choices=QCType.choices)
    label = forms.CharField(max_length=60, help_text="Unique in the batch, for example MB-1.")
    parent_sample = forms.ModelChoiceField(
        queryset=Sample.objects.none(),
        required=False,
        label="Parent sample",
        help_text="Required for matrix spikes and duplicates: the field sample used.",
    )

    def __init__(self, *args, batch, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent_sample"].queryset = (
            Sample.objects.filter(pk__in=batch_sample_ids(batch)).distinct().order_by("lab_id")
        )
        self.fields["parent_sample"].empty_label = "No parent sample"

    def clean(self):
        data = super().clean()
        needs = data.get("qc_type") in NEEDS_PARENT
        if needs and not data.get("parent_sample"):
            self.add_error("parent_sample", "Choose the field sample for this QC type.")
        return data


class QCActionForm(forms.Form):
    target_key = forms.CharField(widget=forms.HiddenInput)
    disposition = forms.ChoiceField(choices=QCCorrectiveAction.Disposition.choices)
    note = forms.CharField(
        label="Corrective action or reason", widget=forms.Textarea(attrs={"rows": 3})
    )


# ----------------------------------------------------------------------- result entry


class BasisForm(forms.Form):
    result_basis = forms.ChoiceField(
        label="Result basis",
        required=False,
        choices=[
            ("", "Choose soil basis"),
            (ResultBasis.WET, "Wet weight"),
            (ResultBasis.DRY, "Dry weight"),
            (ResultBasis.NA, "Not applicable"),
        ],
        help_text="Required for soil. Values and reporting limits are not converted.",
    )
    percent_solids = raw_text(
        "Percent solids (%)", help_text="Optional supplied measurement; blank means not recorded."
    )
    solids_reference = forms.CharField(
        label="Solids source reference",
        required=False,
        max_length=240,
        help_text="Required only with supplied percent solids.",
    )


class ResultRowForm(BasisForm):
    """One row of the batch entry grid. Blank rows are skipped by the view."""

    result_id = forms.IntegerField(widget=forms.HiddenInput)
    prepared_aliquot_id = forms.IntegerField(
        required=False, label="Prepared aliquot", widget=forms.Select
    )
    # No per-row help text: the grid renders only the widgets and its note says "number or ND"
    # and laboratory local time, so a reference to unrendered help would point at nothing.
    supplied_text = raw_text("Result", small=True)
    reporting_limit_applied = raw_text("Reporting limit", small=True)
    dilution_factor = raw_text("Dilution", small=True)
    qualifiers = forms.CharField(
        label="Qualifiers",
        required=False,
        widget=forms.TextInput(attrs={"class": SMALL, "autocomplete": "off", "size": 6}),
    )
    analysis_started_at = local_datetime("Analysis start", required=False, small=True, help_text="")
    reason = forms.CharField(
        label="Reason",
        required=False,
        widget=forms.TextInput(attrs={"class": SMALL, "autocomplete": "off"}),
    )

    def qualifier_codes(self):
        text = self.cleaned_data.get("qualifiers", "").replace(",", " ").replace(";", " ")
        return text.split()

    def is_blank(self):
        """A row with no result, limit or time typed is skipped, not an error."""
        data = self.cleaned_data
        return not (
            (data.get("supplied_text") or "").strip()
            or (data.get("reporting_limit_applied") or "").strip()
            or data.get("analysis_started_at")
        )


RowFormSet = forms.formset_factory(ResultRowForm, extra=0)


class GridDefaultsForm(forms.Form):
    default_analysis_started_at = local_datetime(
        "Default analysis start",
        required=False,
        help_text="Used for rows without their own analysis start. Holding time is evaluated "
        "to this time.",
    )


class CorrectionForm(BasisForm):
    prepared_aliquot_id = forms.IntegerField(
        required=False,
        label="Prepared aliquot",
        widget=forms.Select,
        help_text=(
            "Select the same aliquot for repeat analysis, or a new aliquot for "
            "re-preparation. No identity is inferred from legacy timestamps."
        ),
    )
    supplied_text = raw_text("Result", required=True, help_text="Number or ND")
    reporting_limit_applied = raw_text("Reporting limit applied", required=True)
    dilution_factor = raw_text("Dilution factor", required=True)
    qualifiers = forms.CharField(
        label="Qualifiers", required=False, help_text="Codes separated by commas or spaces."
    )
    analysis_started_at = local_datetime("Analysis start")
    comments = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    reason = forms.CharField(
        label="Reason for correction or reanalysis",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Required when a result already exists. The earlier entry is kept.",
    )

    def qualifier_codes(self):
        return self.cleaned_data.get("qualifiers", "").replace(",", " ").replace(";", " ").split()


# -------------------------------------------------------------------------- QC entry


class QCRowForm(forms.Form):
    definition_id = forms.IntegerField(widget=forms.HiddenInput)
    observed_text = raw_text("Observed", small=True)  # the grid note says "number or ND"
    reporting_limit_applied = raw_text("Reporting limit", small=True)
    spike_value = raw_text("Spike / true value", small=True)
    native_value = raw_text("Unspiked result", small=True)
    reference_value = raw_text("Reference result", small=True)

    def __init__(self, *args, qc_type=None, **kwargs):
        super().__init__(*args, **kwargs)
        if qc_type is not None:
            for name, needs in (
                ("spike_value", NEEDS_SPIKE),
                ("native_value", NEEDS_NATIVE),
                ("reference_value", NEEDS_REFERENCE),
            ):
                if qc_type not in needs:
                    del self.fields[name]


class QCGridCommonForm(forms.Form):
    analysis_started_at = local_datetime("Analysis start")
    analysis_batch_id = forms.ModelChoiceField(
        queryset=Batch.objects.filter(kind="analytical").select_related("method"),
        required=False,
        label="QC analytical run",
        help_text=(
            "Actual analytical batch for preparation QC. Required for its EDD "
            "batch linkage; never inferred."
        ),
    )
    reason = forms.CharField(
        label="Reason for correction",
        required=False,
        help_text="Required when replacing an existing measurement. The earlier one is kept.",
    )


# ------------------------------------------------------------------------------ import


class ImportUploadForm(forms.Form):
    file = forms.FileField(
        label="Results file (CSV, UTF-8)",
        help_text="Required columns: sample_id, analyte, result, reporting_limit, "
        "dilution_factor, analysis_started_at. Optional: unit, qualifiers, comments, "
        "reason, result_basis, percent_solids, solids_reference, prepared_aliquot_id. "
        "Soil requires result_basis wet or dry. "
        "Use ND for non-detects.",
    )


class UploadPayloadField(forms.CharField):
    """The uploaded file as it travels from preview to commit: base64 of the original bytes.
    Cleaned to bytes, bounded by the import size limit, never trimmed or re-encoded."""

    def __init__(self, **kwargs):
        limit = 4 * ((importing.MAX_BYTES + 2) // 3)
        super().__init__(widget=forms.HiddenInput, strip=False, max_length=limit, **kwargs)

    def clean(self, value):
        value = super().clean(value)
        try:
            data = base64.b64decode(value, validate=True)
        except binascii.Error, ValueError:
            raise ValidationError("The file could not be read back. Upload it again.") from None
        if len(data) > importing.MAX_BYTES:
            raise ValidationError(
                f"The file is larger than {importing.MAX_BYTES // 1024} KiB. Upload it again."
            )
        return data


class ImportCommitForm(forms.Form):
    filename = forms.CharField(widget=forms.HiddenInput, max_length=200)
    payload = UploadPayloadField()
    state_token = forms.CharField(widget=forms.HiddenInput, max_length=64)


# ------------------------------------------------------------------- mapped import profiles


class ImportProfileFilterForm(GridFilterForm):
    q = _search("Profile name or note")
    layout = forms.ChoiceField(
        required=False, choices=[("", "Any layout"), *ImportProfile.Layout.choices]
    )
    state = forms.ChoiceField(
        label="State",
        required=False,
        choices=[("", "Any state"), ("active", "Active"), ("inactive", "Inactive")],
    )


def _optional_column(label, help_text=""):
    return forms.CharField(
        label=label, required=False, max_length=120, help_text=help_text, strip=True
    )


class ImportProfileForm(forms.Form):
    """Operator-chosen mapping of a delimited file to result fields. Nothing is assumed from
    a header: every field the import needs is named here or given as a fixed value."""

    name = forms.CharField(max_length=80, label="Profile name")
    layout = forms.ChoiceField(
        choices=[("long", "Long (one result per row)"), ("wide", "Wide (one row per sample)")]
    )
    delimiter = forms.ChoiceField(choices=ImportProfile.Delimiter.choices)
    header_row = forms.IntegerField(
        label="Header row",
        min_value=1,
        max_value=ip.MAX_HEADER_ROW,
        initial=1,
        help_text="The row number holding the column names. Rows above it are ignored and "
        "counted in the preview.",
    )
    date_format = forms.ChoiceField(
        label="Date format in the file",
        choices=list(ip.DATE_FORMATS.items()),
        help_text="Times without a UTC offset are read in the laboratory time zone.",
    )
    sample_column = forms.CharField(
        label="Sample ID column",
        max_length=120,
        help_text="Must hold the laboratory sample ID (for example the one on the label).",
    )
    analysis_column = forms.CharField(label="Analysis start column", max_length=120)
    analyte_column = _optional_column(
        "Analyte column", "Long layout: the column naming the analyte."
    )
    result_column = _optional_column(
        "Result column", "Long layout: the result exactly as the instrument exported it."
    )
    analyte_aliases = forms.CharField(
        label="Analyte names in the file",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "spellcheck": "false"}),
        help_text="Long layout, optional. One per line: name in the file | analyte in the catalog. "
        "A name that is not listed must equal a catalog analyte name exactly.",
    )
    analyte_columns = forms.CharField(
        label="Analyte columns",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "spellcheck": "false"}),
        help_text="Wide layout. One per line: column | catalog analyte | unit in the file | "
        "reporting limit (optional, otherwise the reporting limit below).",
    )
    unit_column = _optional_column("Unit column", "Long layout. Give a column or a fixed unit.")
    unit_fixed = forms.CharField(
        label="Fixed unit",
        required=False,
        max_length=40,
        strip=True,
        help_text="Long layout. The unit this file uses; it must match the test unit. "
        "Units are never converted.",
    )
    reporting_limit_column = _optional_column("Reporting limit column")
    reporting_limit_fixed = raw_text(
        "Fixed reporting limit", help_text="Applied to every row, in the test unit."
    )
    dilution_column = _optional_column("Dilution factor column")
    dilution_fixed = raw_text("Fixed dilution factor", help_text="Applied to every row.")
    result_basis_column = _optional_column(
        "Result basis column", "Soil rows must explicitly supply wet or dry."
    )
    result_basis_fixed = forms.ChoiceField(
        label="Fixed result basis",
        required=False,
        choices=[
            ("", "No fixed basis"),
            ("wet", "Wet weight"),
            ("dry", "Dry weight"),
            ("na", "Not applicable"),
        ],
    )
    percent_solids_column = _optional_column(
        "Percent solids column", "Optional exact percentage; blank is not 100%."
    )
    percent_solids_fixed = raw_text(
        "Fixed percent solids (%)",
        help_text="Only when the same measured value actually applies to "
        "every imported observation.",
    )
    solids_reference_column = _optional_column("Solids reference column")
    solids_reference_fixed = forms.CharField(
        label="Fixed solids source reference", required=False, max_length=240
    )
    qualifiers_column = _optional_column("Qualifiers column", "Optional. Codes separated by ; or ,")
    comments_column = _optional_column("Comments column", "Optional.")
    prepared_aliquot_column = _optional_column(
        "Prepared aliquot ID column",
        "Explicit LIMS aliquot ID; required when the test requires independent preparation. "
        "No automatic selection.",
    )
    reason_column = _optional_column(
        "Reason column", "Optional. Needed to replace an existing result."
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    is_active = forms.BooleanField(required=False, initial=True, label="Active")

    FIELDS_BY_LAYOUT = {
        "long": ("analyte_column", "result_column"),
        "wide": ("analyte_columns",),
    }

    def __init__(self, *args, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not editing:
            del self.fields["is_active"]

    @classmethod
    def initial_for(cls, profile):
        m = profile.mapping
        initial = {
            "name": profile.name,
            "layout": profile.layout,
            "delimiter": profile.delimiter,
            "notes": profile.notes,
            "is_active": profile.is_active,
            "header_row": m.get("header_row", 1),
            "date_format": m.get("date_format"),
            "sample_column": m.get("sample", ""),
            "analysis_column": m.get("analysis_started_at", ""),
            "analyte_column": m.get("analyte", ""),
            "result_column": m.get("result", ""),
            "qualifiers_column": m.get("qualifiers") or "",
            "comments_column": m.get("comments") or "",
            "reason_column": m.get("reason") or "",
            "prepared_aliquot_column": m.get("prepared_aliquot_id") or "",
            "analyte_aliases": "\n".join(
                f"{k} {ip.SEPARATOR} {v}" for k, v in (m.get("analyte_aliases") or {}).items()
            ),
            "analyte_columns": "\n".join(
                f" {ip.SEPARATOR} ".join(
                    [c["column"], c["analyte"], c["unit"], c.get("reporting_limit", "")]
                ).removesuffix(f" {ip.SEPARATOR} ")
                for c in m.get("analyte_columns", [])
            ),
        }
        for role, prefix in (
            ("unit", "unit"),
            ("reporting_limit", "reporting_limit"),
            ("dilution_factor", "dilution"),
            ("result_basis", "result_basis"),
            ("percent_solids", "percent_solids"),
            ("solids_reference", "solids_reference"),
        ):
            spec = m.get(role) or {}
            initial[f"{prefix}_column"] = spec.get("column", "")
            initial[f"{prefix}_fixed"] = spec.get("fixed", "")
        return initial

    def _lines(self, field, parts, label):
        rows = []
        for number, line in enumerate((self.cleaned_data.get(field) or "").splitlines(), 1):
            if not line.strip():
                continue
            cells = [c.strip() for c in line.split(ip.SEPARATOR)]
            if not parts[0] <= len(cells) <= parts[1] or not all(cells[: parts[0]]):
                self.add_error(field, f"Line {number}: use {label}.")
                continue
            rows.append(cells)
        return rows

    def _value_role(self, name, prefix):
        column, fixed = (
            self.cleaned_data.get(f"{prefix}_column"),
            self.cleaned_data.get(f"{prefix}_fixed"),
        )
        if column and (fixed or "").strip():
            self.add_error(f"{prefix}_fixed", "Give a column or a fixed value, not both.")
            return None
        if column:
            return {"column": column}
        if (fixed or "").strip():
            return {"fixed": fixed.strip()}
        return None

    def clean(self):
        data = super().clean()
        layout = data.get("layout")
        if not layout:
            return data
        mapping = {
            "header_row": data.get("header_row"),
            "date_format": data.get("date_format"),
            "sample": data.get("sample_column"),
            "analysis_started_at": data.get("analysis_column"),
        }
        for role, prefix in (
            ("unit", "unit"),
            ("reporting_limit", "reporting_limit"),
            ("dilution_factor", "dilution"),
            ("result_basis", "result_basis"),
            ("percent_solids", "percent_solids"),
            ("solids_reference", "solids_reference"),
        ):
            spec = self._value_role(role, prefix)
            if spec:
                mapping[role] = spec
        for role, field in (
            ("qualifiers", "qualifiers_column"),
            ("comments", "comments_column"),
            ("reason", "reason_column"),
            ("prepared_aliquot_id", "prepared_aliquot_column"),
        ):
            if data.get(field):
                mapping[role] = data[field]
        if layout == "long":
            mapping["analyte"], mapping["result"] = (
                data.get("analyte_column"),
                data.get("result_column"),
            )
            aliases = {}
            for label, analyte in self._lines(
                "analyte_aliases", (2, 2), "file name | catalog analyte"
            ):
                if label.casefold() in {a.casefold() for a in aliases}:
                    self.add_error("analyte_aliases", f"'{label}' is listed twice.")
                aliases[label] = analyte
            if aliases:
                mapping["analyte_aliases"] = aliases
            if data.get("analyte_columns", "").strip():
                self.add_error("analyte_columns", "Analyte columns apply to the wide layout only.")
        else:
            entries = []
            for cells in self._lines(
                "analyte_columns", (3, 4), "column | catalog analyte | unit [| reporting limit]"
            ):
                entry = {"column": cells[0], "analyte": cells[1], "unit": cells[2]}
                if len(cells) == 4 and cells[3]:
                    entry["reporting_limit"] = cells[3]
                entries.append(entry)
            mapping["analyte_columns"] = entries
            for field in (
                "analyte_column",
                "result_column",
                "analyte_aliases",
                "unit_column",
                "unit_fixed",
            ):
                if (data.get(field) or "").strip():
                    self.add_error(field, "This applies to the long layout only.")
        for field, label in (
            ("reporting_limit_fixed", "Reporting limit"),
            ("dilution_fixed", "Dilution factor"),
        ):
            text = (data.get(field) or "").strip()
            if text:
                try:
                    if parse_decimal(text, label) <= 0:
                        raise ValidationError(f"{label} must be greater than zero.")
                except ValidationError as exc:
                    self.add_error(field, exc)
        if not self.errors:
            for message in ip.validate_mapping(layout, mapping):
                self.add_error(None, message)
        self.mapping = mapping
        return data


class MappedImportUploadForm(forms.Form):
    profile = forms.ModelChoiceField(
        queryset=ImportProfile.objects.none(),
        label="Mapping profile",
        empty_label="Choose a profile",
        help_text="How the columns of this file map to results.",
    )
    file = forms.FileField(label="Results file (UTF-8 text)")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["profile"].queryset = ImportProfile.objects.filter(is_active=True)


class MappedImportCommitForm(forms.Form):
    filename = forms.CharField(widget=forms.HiddenInput, max_length=200)
    payload = UploadPayloadField()
    profile_id = forms.IntegerField(widget=forms.HiddenInput)
    mapping_hash = forms.CharField(widget=forms.HiddenInput, max_length=64)
    state_token = forms.CharField(widget=forms.HiddenInput, max_length=64)


# ------------------------------------------------------------------------------ review


class ReviewForm(forms.Form):
    """Decision on the selected results. Selected revision ids are read from the checkboxes."""

    action = forms.ChoiceField(choices=[("approve", "Approve"), ("return", "Return")])
    note = forms.CharField(
        required=False,
        label="Note or reason",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Required to return a result. Optional when approving.",
    )

    def clean(self):
        data = super().clean()
        if data.get("action") == "return" and not (data.get("note") or "").strip():
            self.add_error("note", "A reason is required to return results.")
        return data


MAX_ID = 2**63 - 1  # PostgreSQL bigint


def parse_id(value):
    """A record id from posted text, or None. Only ASCII digits within the bigint range count:
    str.isdigit() also accepts characters such as '²' that int() rejects."""
    text = str(value or "")
    if text.isascii() and text.isdigit() and len(text) <= 19 and 0 < int(text) <= MAX_ID:
        return int(text)
    return None


def posted_ids(request, name):
    """Integer ids from repeated checkbox values; anything else is ignored."""
    return sorted({i for v in request.POST.getlist(name) if (i := parse_id(v)) is not None})


# ------------------------------------------------------------------------ configuration


def hours_field(label, required, help_text):
    return forms.DecimalField(
        label=label,
        required=required,
        min_value=Decimal("0.01"),
        decimal_places=2,
        max_digits=9,
        help_text=help_text,
    )


class ReasonMixin(forms.Form):
    reason = forms.CharField(
        required=False,
        label="Reason for change",
        help_text="Required when changing an existing row.",
    )


class HoldingRuleForm(forms.ModelForm):
    duration_hours = hours_field(
        "Maximum time (hours)",
        True,
        "From the start event to the end event. 0.25 = 15 minutes; 48 = 2 days; 672 = 28 days.",
    )
    warning_hours = hours_field(
        "Warn when remaining (hours)", False, "Flag as at risk when this much time or less remains."
    )
    reason = ReasonMixin.base_fields["reason"]

    class Meta:
        model = HoldingTimeRule
        fields = ["method", "matrix", "name", "start_event", "end_event", "basis", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["duration_hours"].initial = _hours(self.instance.duration)
            self.fields["warning_hours"].initial = _hours(self.instance.warning_window)
        name_empty_choices(self)
        self.order_fields(
            [
                "method",
                "matrix",
                "name",
                "start_event",
                "end_event",
                "duration_hours",
                "warning_hours",
                "basis",
                "is_active",
                "reason",
            ]
        )

    def clean(self):
        data = super().clean()
        for name in ("duration_hours", "warning_hours"):
            if data.get(name) is not None:
                seconds = (data[name] * 3600).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                data[name.replace("hours", "td")] = timedelta(seconds=int(seconds))
        return data

    def save(self, commit=True):
        self.instance.duration = self.cleaned_data["duration_td"]
        self.instance.warning_window = self.cleaned_data.get("warning_td")
        return super().save(commit=commit)


def _hours(delta):
    if delta is None:
        return None
    try:
        return (Decimal(delta.total_seconds()) / 3600).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


class QCRequirementForm(forms.ModelForm):
    reason = ReasonMixin.base_fields["reason"]

    class Meta:
        model = QCRequirement
        fields = ["method", "qc_type", "min_per_batch", "every_n_samples", "basis", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        name_empty_choices(self)


class QCLimitForm(forms.ModelForm):
    reason = ReasonMixin.base_fields["reason"]

    class Meta:
        model = QCLimit
        fields = [
            "definition",
            "phase",
            "qc_type",
            "measure",
            "lower",
            "upper",
            "basis",
            "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["definition"].queryset = TestDefinition.objects.select_related(
            "method", "analyte"
        ).order_by("method__code", "analyte__name", "matrix")
        self.fields["definition"].empty_label = "Choose a test"
        for name in ("lower", "upper"):  # show limits without storage-only trailing zeros
            value = getattr(self.instance, name, None)
            if self.instance.pk and value is not None:
                self.initial[name] = trim(value)
        self.fields[
            "lower"
        ].help_text = (
            "Same unit as the measure: result unit for value, percent for recovery and RPD."
        )
        name_empty_choices(self)


class QualifierForm(forms.ModelForm):
    reason = ReasonMixin.base_fields["reason"]

    class Meta:
        model = Qualifier
        fields = ["code", "description", "indicates_holding_time", "is_active"]


class PreparationRequirementForm(forms.ModelForm):
    reason = ReasonMixin.base_fields["reason"]

    class Meta:
        model = PreparationRequirement
        fields = ["definition", "method", "basis", "is_active"]
        labels = {"method": "Required preparation method"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["definition"].queryset = TestDefinition.objects.select_related(
            "method", "analyte"
        )
        self.fields["method"].queryset = Method.objects.filter(is_active=True)


class PreservationRuleForm(forms.ModelForm):
    reason = ReasonMixin.base_fields["reason"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["definition"].queryset = TestDefinition.objects.select_related(
            "method", "analyte"
        ).order_by("method__code", "analyte__name", "matrix")
        self.fields["definition"].empty_label = "Choose a test"

    class Meta:
        model = PreservationRule
        fields = [
            "definition",
            "temperature_mode",
            "lower",
            "upper",
            "unit",
            "container_requirements",
            "chemical_requirements",
            "other_requirements",
            "basis",
            "is_active",
        ]
        labels = {
            "lower": "Minimum cooler receipt temperature",
            "upper": "Maximum cooler receipt temperature",
        }
        help_texts = {
            "definition": "Exact method version, analyte and matrix; no inferred rules.",
            "temperature_mode": "Screens recorded cooler temperature only, not continuous cooling.",
            "container_requirements": "Manual checks; narrative is never parsed into a pass.",
            "chemical_requirements": "Manual checks; pH is never inferred from text.",
            "basis": (
                "Method/SOP source and applicability. Appears on issued PDFs and client copies; "
                "editing this text invalidates prior matching assessments."
            ),
        }
        widgets = {
            name: forms.Textarea(attrs={"rows": 3})
            for name in (
                "container_requirements",
                "chemical_requirements",
                "other_requirements",
                "basis",
            )
        }


CONFIG_KINDS = {
    "preparation-requirement": (
        PreparationRequirement,
        PreparationRequirementForm,
        "Preparation requirement",
    ),
    "preservation-rule": (PreservationRule, PreservationRuleForm, "Preservation rule"),
    "holding-rule": (HoldingTimeRule, HoldingRuleForm, "Holding-time rule"),
    "qc-requirement": (QCRequirement, QCRequirementForm, "QC requirement"),
    "qc-limit": (QCLimit, QCLimitForm, "QC limit"),
    "qualifier": (Qualifier, QualifierForm, "Qualifier"),
}


# --------------------------------------------------------------------- MDL studies


class MDLFilterForm(GridFilterForm):
    q = _search("Analyte, method or study number")
    method = _method_filter()
    matrix = forms.ChoiceField(choices=MATRIX_FILTER, required=False)
    kind = forms.ChoiceField(
        label="Kind", required=False, choices=[("", "Any kind"), *MDLStudy.Kind.choices]
    )
    state = forms.ChoiceField(
        label="State",
        required=False,
        choices=[
            ("", "Any state"),
            ("no_calculation", "No calculation yet"),
            ("not_evaluated", "Last calculation not evaluated"),
            ("pending_review", "Awaiting review"),
            ("rejected", "Last calculation rejected"),
            ("approved", "Current MDL in place"),
        ],
    )
    due = forms.ChoiceField(
        label="Recalculation",
        required=False,
        choices=[("", "Any"), ("overdue", "Past 13 months since calculation")],
    )
    status = forms.ChoiceField(
        label="Study status",
        required=False,
        choices=[("", "Any"), *MDLStudy.Status.choices],
    )


class MDLStudyForm(forms.Form):
    definition = forms.ModelChoiceField(
        label="Test (method, analyte, matrix)",
        queryset=TestDefinition.objects.none(),
        help_text="The MDL is determined per method, analyte and sample matrix.",
    )
    kind = forms.ChoiceField(choices=MDLStudy.Kind.choices)
    spiking_level = raw_text(
        "Spiking level",
        required=True,
        help_text="In the test unit; typically 2 to 10 times the "
        "estimated MDL (Appendix B II.2(a)). Use one level for every spike.",
    )
    instrument_note = forms.CharField(
        required=False,
        max_length=200,
        label="Instruments covered",
        help_text="If several instruments share this MDL, name them; each needs its own data.",
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["definition"].queryset = (
            TestDefinition.objects.filter(is_active=True, method__is_active=True)
            .select_related("method", "analyte")
            .order_by("method__code", "analyte__name", "matrix")
        )
        self.fields["definition"].empty_label = "Choose a test"
        name_empty_choices(self)


class MDLObservationForm(forms.Form):
    kind = forms.ChoiceField(choices=MDLObservation.Kind.choices, label="Sample type")
    supplied_text = raw_text(
        "Result", required=True, help_text="A number, or ND for not detected (blanks)."
    )
    spike_level = raw_text(
        "Spiking level", help_text="Spiked samples only; must equal the study's level."
    )
    qualitative_ok = forms.ChoiceField(
        label="Qualitative identification met",
        required=False,
        choices=[("", "Spiked samples: choose"), ("yes", "Yes"), ("no", "No")],
        help_text="Required for spiked samples (Appendix B II.2(c)).",
    )
    prepared_at = local_datetime("Prepared")
    analyzed_at = local_datetime("Analysed")
    batch_label = forms.CharField(max_length=60, label="Preparation batch")
    preparation_label = forms.CharField(
        max_length=60,
        label="Preparation / extract ID",
        help_text="One extract analysed on several instruments is one preparation: give it the "
        "same ID each time (Appendix B II.2(b)(iii)).",
    )
    instrument = forms.CharField(max_length=80, required=False)


class MDLImportBlanksForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["start"].required = self.fields["end"].required = True

    start = date_field("From")
    end = date_field("To")
    basis = forms.ChoiceField(
        label="Routine blank source basis",
        choices=[
            ("preparation", "Preparation-batch blanks"),
            ("analytical", "Analytical / combined full-procedure blanks"),
        ],
        help_text="Import one source basis at a time. Confirm blanks underwent every method "
        "processing step; a batch label alone does not establish eligibility.",
    )
    reason = forms.CharField(
        label="Full-procedure evidence for alternative basis",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Required when choosing a different basis from the current preparation "
        "requirement. Explain the actual processing; retained with every imported blank.",
    )


class MDLExcludeForm(forms.Form):
    reason = forms.CharField(
        label="Documented gross failure",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="For example an instrument malfunction, mislabelled sample or cracked vial. "
        "Statistical outlier removal is not permitted (Appendix B II.2(b)).",
    )


class MDLCalculateForm(forms.Form):
    blank_window = forms.ChoiceField(
        label="Method blanks used",
        choices=[
            ("24_months", "All from the last 24 months"),
            ("recent", "Optional: last 6 months or 50 most recent, whichever is more"),
        ],
        help_text="Appendix B II.4(e).",
    )


class MDLReviewForm(forms.Form):
    decision = forms.ChoiceField(choices=MDL_DECISIONS)
    note = forms.CharField(
        required=False,
        label="Reason or note",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Required to keep the existing MDL or to reject.",
    )


class ChartFilterForm(GridFilterForm):
    q = _search("Analyte or method")
    phase = forms.ChoiceField(
        label="QC phase",
        choices=[("analysis", "Analytical QC"), ("preparation", "Preparation QC")],
        required=False,
        initial="analysis",
    )
    method = _method_filter()
    matrix = forms.ChoiceField(choices=MATRIX_FILTER, required=False)
    qc_type = forms.ChoiceField(
        label="QC type", required=False, choices=[("", "Any QC type"), *QCType.choices]
    )


class ChartBaselineForm(forms.Form):
    phase = forms.ChoiceField(
        label="QC phase",
        choices=[("analysis", "Analytical QC"), ("preparation", "Preparation QC")],
        required=False,
        initial="analysis",
    )
    baseline = forms.ChoiceField(
        label="Statistical baseline",
        required=False,
        choices=[
            ("30", "Most recent 30 points"),
            ("20", "Most recent 20 points"),
            ("50", "Most recent 50 points"),
            ("0", "Every point"),
        ],
        help_text="Descriptive limits are the mean of these points plus or minus 2 and 3 standard "
        "deviations.",
    )
