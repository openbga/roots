from datetime import datetime
from decimal import Decimal

from django import forms

from ..models import NysdecProjectSettings, NysdecQualifierMapping, NysdecTestMapping
from .nysdec_format import ENUMERATIONS
from .services import PROJECT_FIELDS, TEST_FIELDS

CODE_HELP = "Exact NYSDEC valid value (reference values 20260429); checked only by EQuIS EDP."


class NysdecProjectForm(forms.ModelForm):
    task_code = forms.ChoiceField(
        required=False,
        choices=[("", "Not set")] + [(v, v) for v in ENUMERATIONS["task_code"]],
        help_text="Project phase code from the NYSDEC format (SC, RD, RIFS, RC, IRM, SM, RSO).",
    )

    class Meta:
        model = NysdecProjectSettings
        fields = PROJECT_FIELDS
        help_texts = {
            "data_provider": "Registered rt_company code of the party submitting the EDD.",
            "sampling_company_code": "Registered rt_company code of the company that sampled.",
            "lab_name_code": "Registered rt_company code of this laboratory.",
            "water_matrix_code": "rt_matrix code for this project's water samples "
            "(for example surface water or groundwater; they are different codes).",
            "wastewater_matrix_code": "rt_matrix code for this project's wastewater samples.",
            "soil_matrix_code": "rt_matrix code for this project's soil samples.",
            "water_qc_matrix_code": "rt_matrix LAB-class code for laboratory QC samples of "
            "water batches (for example Water Analytical).",
            "wastewater_qc_matrix_code": "rt_matrix LAB-class code for wastewater batch QC.",
            "soil_qc_matrix_code": "rt_matrix LAB-class code for soil batch QC.",
        }


class NysdecTestForm(forms.ModelForm):
    class Meta:
        model = NysdecTestMapping
        fields = TEST_FIELDS
        help_texts = {
            **{k: CODE_HELP for k in TEST_FIELDS if k != "basis"},
            "preparation_method_code": (
                "Exact laboratory preparation method code from the "
                "frozen report; not the NYSDEC code."
            ),
            "preparation_method_version": (
                "Exact laboratory preparation method version from the frozen report."
            ),
            "prep_method": (
                "Explicit rt_prep_method code for that preparation method/version. "
                "No method is inferred."
            ),
        }


class NysdecQualifierForm(forms.ModelForm):
    class Meta:
        model = NysdecQualifierMapping
        fields = ("interpreted_qualifier",)
        help_texts = {"interpreted_qualifier": "rt_qualifier code. " + CODE_HELP}


DEPTH_UNITS = [("", "—")] + [(u, u) for u in ENUMERATIONS["in_ft_unit"]]


class VersionContextForm(forms.Form):
    """Per-sample depth/location and per-result test type for one released version. Fields are
    built from the frozen snapshot: one row per reported sample, one per corrected result whose
    analysis start differs from revision 1 (`needs_test_type`)."""

    def __init__(
        self,
        *args,
        samples,
        results,
        initial_samples,
        initial_results,
        qc_rows=(),
        initial_qc=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.sample_rows, self.result_rows, self.qc_rows = [], [], []
        initial_qc = initial_qc or {}
        for qc in qc_rows:
            code, saved = qc["code"], initial_qc.get(qc["code"], {})
            date_name, df_name = f"qc_sample_date__{code}", f"qc_dilution_factor__{code}"
            self.fields[date_name] = forms.DateTimeField(
                required=False,
                label=f"{code} sample date",
                widget=forms.DateTimeInput(
                    attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
                ),
                initial=saved.get("sample_date") and datetime.fromisoformat(saved["sample_date"]),
            )
            self.fields[df_name] = forms.DecimalField(
                required=False,
                min_value=Decimal("0.0001"),
                max_digits=14,
                decimal_places=4,
                label=f"{code} dilution factor",
                initial=saved.get("dilution_factor"),
            )
            self.qc_rows.append((qc, self[date_name], self[df_name]))
        for s in samples:
            lab_id, saved = s["lab_id"], initial_samples.get(s["lab_id"], {})
            names = {
                k: f"{k}__{lab_id}"
                for k in ("start_depth", "end_depth", "depth_unit", "sys_loc_code")
            }
            self.fields[names["start_depth"]] = forms.DecimalField(
                required=False,
                min_value=0,
                max_digits=12,
                decimal_places=4,
                label=f"{lab_id} start depth",
                initial=saved.get("start_depth"),
            )
            self.fields[names["end_depth"]] = forms.DecimalField(
                required=False,
                min_value=0,
                max_digits=12,
                decimal_places=4,
                label=f"{lab_id} end depth",
                initial=saved.get("end_depth"),
            )
            self.fields[names["depth_unit"]] = forms.ChoiceField(
                required=False,
                choices=DEPTH_UNITS,
                label=f"{lab_id} depth unit",
                initial=saved.get("depth_unit", ""),
            )
            self.fields[names["sys_loc_code"]] = forms.CharField(
                required=False,
                max_length=20,
                label=f"{lab_id} NYSDEC location code",
                initial=saved.get("sys_loc_code", ""),
            )
            self.sample_rows.append((s, {k: self[n] for k, n in names.items()}))
        for r in results:
            name = f"test_type__{r['revision_id']}"
            self.fields[name] = forms.CharField(
                required=False,
                max_length=10,
                label=f"{r['lab_id']} {r['analyte']} revision {r['revision']} test type",
                initial=initial_results.get(str(r["revision_id"]), {}).get("test_type", ""),
                help_text="rt_test_type code, for example REANALYSIS or REEXTRACT; checked by EDP.",
            )
            self.result_rows.append((r, self[name]))

    def values(self):
        data = self.cleaned_data
        samples = {}
        for s, _ in self.sample_rows:
            row = {}
            for k in ("start_depth", "end_depth"):
                v = data.get(f"{k}__{s['lab_id']}")
                row[k] = "" if v is None else format(v.normalize(), "f")
            row["depth_unit"] = data.get(f"depth_unit__{s['lab_id']}", "")
            row["sys_loc_code"] = (data.get(f"sys_loc_code__{s['lab_id']}") or "").strip()
            samples[s["lab_id"]] = row
        results = {
            str(r["revision_id"]): {
                "test_type": (data.get(f"test_type__{r['revision_id']}") or "").strip().upper()
            }
            for r, _ in self.result_rows
        }
        qc = {}
        for row, date_field, df_field in self.qc_rows:
            when, factor = data.get(date_field.name), data.get(df_field.name)
            qc[row["code"]] = {
                "sample_date": when.isoformat() if when else "",
                "dilution_factor": "" if factor is None else format(factor.normalize(), "f"),
            }
        return samples, results, qc
