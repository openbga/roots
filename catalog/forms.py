from django import forms

from .models import Analyte, Method, TestDefinition
from .services import analyte_locks, definition_locks, method_locks

MATRICES = [("water", "Water"), ("wastewater", "Wastewater"), ("soil", "Soil")]
UNIT_SUGGESTIONS = ["mg/L", "µg/L", "ng/L", "mg/kg", "µg/kg", "SU", "µS/cm", "NTU", "%"]
PAGE_SIZES = [(size, f"{size} rows") for size in (25, 50, 100)]
ACTIVE_LABEL = "Active (available for new work)"
REASON_HELP = "Required if you change anything. Kept in the audit history."
NOT_RECORDED = "Not recorded"


def descriptor_choices(choices, empty):
    return [("", empty), *choices]


class GroupedForm:
    """Fieldsets for the shared form template: [(legend, [bound fields])], skipping empty ones."""

    groups = ()

    def grouped(self):
        out = []
        for legend, names in self.groups:
            fields = [self[name] for name in names if name in self.fields]
            if fields:
                out.append((legend, fields))
        return out


class RecordForm(GroupedForm, forms.ModelForm):
    """Edit form that shows locked identity values as facts and asks for a reason only when
    an existing record is changed. The service enforces the same lock and reason rules."""

    lock_check = None
    identity = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.locks = type(self).lock_check(self.instance) if self.instance.pk else []
        self.locked_values = []
        if self.locks:
            for name in self.identity:
                field = self.fields.pop(name)
                self.locked_values.append((field.label or name.title(), self._display(name)))
        if self.instance.pk:
            self.fields["reason"] = forms.CharField(
                required=False,
                label="Reason for change",
                help_text=REASON_HELP,
                widget=forms.Textarea(attrs={"rows": 2}),
            )

    def _display(self, name):
        method = getattr(self.instance, f"get_{name}_display", None)
        value = method() if method else getattr(self.instance, name)
        return value or "—"

    def clean(self):
        data = super().clean()
        if self.instance.pk:
            changed = [
                name for name in self.changed_data if name not in {"reason", "original_state"}
            ]
            if changed and not (data.get("reason") or "").strip():
                self.add_error("reason", "Give the reason for this change.")
        return data


class MethodForm(RecordForm):
    lock_check = staticmethod(method_locks)
    identity = ("code", "version", "title", "source")
    groups = (
        ("Method version", ["code", "version", "title"]),
        ("Provenance", ["source"]),
        ("Classification", ["reference", "technique"]),
        ("Availability", ["is_active"]),
        ("Change record", ["reason"]),
    )

    class Meta:
        model = Method
        fields = ["code", "version", "title", "source", "reference", "technique", "is_active"]
        labels = {
            "code": "Method code",
            "version": "Version",
            "source": "Source or SOP reference",
            "is_active": ACTIVE_LABEL,
        }
        help_texts = {
            "code": "For example EPA 200.8 or SM 4500-NH3 G.",
            "version": "Free text: revision, edition or year, exactly as the lab names it.",
            "source": "Where this version comes from: citation, SOP number and revision.",
            "reference": "Who publishes the method. Used to filter the catalog; not printed "
            "on reports.",
            "technique": "The measurement technique. Used to filter the catalog and group work; "
            "not printed on reports.",
            "is_active": "Inactive versions cannot be chosen for new samples or batches. "
            "Existing work keeps pointing at it.",
        }
        widgets = {"source": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, choices in (
            ("reference", Method.Reference.choices),
            ("technique", Method.Technique.choices),
        ):
            self.fields[name].choices = descriptor_choices(choices, NOT_RECORDED)


class NewVersionForm(GroupedForm, forms.Form):
    groups = (
        ("New version", ["version", "title", "source"]),
        ("Classification", ["reference", "technique"]),
        ("What happens to existing records", ["copy_tests", "retire_previous"]),
        ("Change record", ["reason"]),
    )
    version = forms.CharField(
        max_length=40, help_text="Free text: the new revision, edition or year."
    )
    title = forms.CharField(max_length=200)
    source = forms.CharField(
        required=False,
        label="Source or SOP reference",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    reference = forms.ChoiceField(
        required=False,
        label="Reference source",
        choices=descriptor_choices(Method.Reference.choices, NOT_RECORDED),
        help_text="Prefilled from the previous version. Who publishes the method. Used to "
        "filter the catalog; not printed on reports.",
    )
    technique = forms.ChoiceField(
        required=False,
        label="Technique",
        choices=descriptor_choices(Method.Technique.choices, NOT_RECORDED),
        help_text="Prefilled from the previous version. Used to filter the catalog and group "
        "work; not printed on reports.",
    )
    copy_tests = forms.BooleanField(
        required=False,
        initial=True,
        label="Copy the active tests to the new version",
        help_text="Copies analyte, matrix, unit and reporting limit. QC limits and holding "
        "rules belong to each version and are set again on the QC configuration page.",
    )
    retire_previous = forms.BooleanField(
        required=False,
        label="Retire the previous version",
        help_text="If left unticked, both versions stay active and receipt offers tests "
        "from both. Existing results keep pointing at the old version either way.",
    )
    reason = forms.CharField(
        label="Reason for the new version", widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, previous, **kwargs):
        super().__init__(*args, **kwargs)
        self.previous = previous
        self.fields["title"].initial = previous.title
        self.fields["source"].initial = previous.source
        self.fields["reference"].initial = previous.reference
        self.fields["technique"].initial = previous.technique
        active = previous.testdefinition_set.filter(is_active=True).count()
        self.fields["copy_tests"].label = f"Copy the {active} active test{'s' * (active != 1)}"
        if previous.is_active:
            self.fields["retire_previous"].label = f"Retire {previous} for new work"


class AnalyteForm(RecordForm):
    lock_check = staticmethod(analyte_locks)
    identity = ("name", "cas_number")
    groups = (
        ("Analyte", ["name", "cas_number"]),
        ("Classification", ["group"]),
        ("Change record", ["reason"]),
    )

    class Meta:
        model = Analyte
        fields = ["name", "cas_number", "group"]
        labels = {"name": "Analyte name", "cas_number": "CAS number"}
        help_texts = {
            "cas_number": "Optional, for example 7439-92-1. The check digit is verified.",
            "group": "Used to filter the catalog and group work; not printed on reports.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group"].choices = descriptor_choices(Analyte.Group.choices, NOT_RECORDED)


class DefinitionForm(RecordForm):
    lock_check = staticmethod(definition_locks)
    identity = ("method", "analyte", "matrix", "unit")
    groups = (
        ("What is measured", ["method", "analyte", "matrix"]),
        ("Reporting", ["unit", "reporting_limit"]),
        ("Availability", ["is_active"]),
        ("Change record", ["reason"]),
    )

    class Meta:
        model = TestDefinition
        fields = ["method", "analyte", "matrix", "unit", "reporting_limit", "is_active"]
        labels = {"reporting_limit": "Default reporting limit", "is_active": ACTIVE_LABEL}
        help_texts = {
            "unit": "Stored exactly as typed and never converted. Reuse a suggestion so the "
            "lab spells each unit one way.",
            "reporting_limit": "In the unit above, exact to 8 decimal places. Each result stores "
            "the limit actually applied. Leave blank if the lab sets it per result.",
            "is_active": "Inactive tests cannot be requested on new samples.",
        }
        widgets = {
            "unit": forms.TextInput(attrs={"list": "unit-suggestions", "autocomplete": "off"}),
            "reporting_limit": forms.TextInput(attrs={"inputmode": "decimal"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "method" in self.fields:
            methods = Method.objects.filter(is_active=True)
            if self.instance.pk:
                methods = Method.objects.filter(is_active=True) | Method.objects.filter(
                    pk=self.instance.method_id
                )
            self.fields["method"].queryset = methods.order_by("code", "pk")
            self.fields["method"].label_from_instance = lambda m: f"{m} — {m.title}"
        if "analyte" in self.fields:
            self.fields["analyte"].queryset = Analyte.objects.order_by("name")
            self.fields["analyte"].label_from_instance = lambda a: (
                f"{a.name} ({a.cas_number})" if a.cas_number else a.name
            )
        if "matrix" in self.fields:
            self.fields["matrix"].choices = [("", "---------"), *MATRICES]
        existing = list(
            TestDefinition.objects.order_by("unit").values_list("unit", flat=True).distinct()
        )
        self.unit_suggestions = existing + [u for u in UNIT_SUGGESTIONS if u not in existing]
        limit = self.instance.reporting_limit
        if limit is not None:  # show the exact value without storage-scale zeros
            self.initial["reporting_limit"] = format(limit.normalize(), "f")


# ---------------------------------------------------------------------------- filters


class GridFilter(forms.Form):
    """Shared query vocabulary: q, per_page, hidden sort. Invalid input is never widened."""

    q = forms.CharField(label="Search", required=False, max_length=200, widget=forms.SearchInput())
    per_page = forms.TypedChoiceField(
        label="Rows per page", choices=PAGE_SIZES, coerce=int, required=False
    )
    sort = forms.CharField(required=False, widget=forms.HiddenInput())

    def distinct_choices(self, field, values, empty):
        self.fields[field].choices = [("", empty), *((v, v) for v in values)]


YES_NO = [("", "Any"), ("yes", "Yes"), ("no", "No")]


class DefinitionFilterForm(GridFilter):
    method = forms.ModelChoiceField(
        queryset=Method.objects.order_by("code", "pk"), required=False, empty_label="Any version"
    )
    code = forms.ChoiceField(label="Method code", required=False)
    analyte = forms.ModelChoiceField(
        queryset=Analyte.objects.order_by("name"), required=False, empty_label="Any analyte"
    )
    matrix = forms.ChoiceField(choices=[("", "Any matrix"), *MATRICES], required=False)
    unit = forms.ChoiceField(required=False)
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Any status"),
            ("available", "Available at receipt"),
            ("active", "Test active"),
            ("inactive", "Test inactive"),
        ],
    )
    limit = forms.ChoiceField(
        label="Default limit",
        required=False,
        choices=[("", "Any"), ("set", "Has a default limit"), ("none", "No default limit")],
    )
    group = forms.ChoiceField(
        label="Analyte group",
        required=False,
        choices=descriptor_choices(Analyte.Group.choices, "Any group"),
    )
    used = forms.ChoiceField(label="Requested on samples", required=False, choices=YES_NO)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget.attrs["placeholder"] = "Analyte, method code or title"
        self.distinct_choices(
            "code",
            Method.objects.order_by("code").values_list("code", flat=True).distinct(),
            "Any code",
        )
        self.distinct_choices(
            "unit",
            TestDefinition.objects.order_by("unit").values_list("unit", flat=True).distinct(),
            "Any unit",
        )


class MethodFilterForm(GridFilter):
    code = forms.ChoiceField(label="Method code", required=False)
    status = forms.ChoiceField(
        required=False,
        choices=[("", "Any status"), ("active", "Active"), ("inactive", "Inactive")],
    )
    reference = forms.ChoiceField(
        label="Reference source",
        required=False,
        choices=descriptor_choices(Method.Reference.choices, "Any reference"),
    )
    technique = forms.ChoiceField(
        required=False, choices=descriptor_choices(Method.Technique.choices, "Any technique")
    )
    used = forms.ChoiceField(label="Used by work", required=False, choices=YES_NO)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget.attrs["placeholder"] = "Code, version, title or source"
        self.distinct_choices(
            "code",
            Method.objects.order_by("code").values_list("code", flat=True).distinct(),
            "Any code",
        )


class AnalyteFilterForm(GridFilter):
    group = forms.ChoiceField(
        label="Group",
        required=False,
        choices=descriptor_choices(Analyte.Group.choices, "Any group"),
    )
    tests = forms.ChoiceField(
        label="Tests",
        required=False,
        choices=[("", "Any"), ("any", "Has tests"), ("none", "No tests yet")],
    )
    used = forms.ChoiceField(label="Requested on samples", required=False, choices=YES_NO)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget.attrs["placeholder"] = "Name or CAS number"
