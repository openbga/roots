"""Native requested-test options with explicit metadata for progressive enhancement."""

import json

from django import forms


def test_option_data(definition):
    return {
        "value": str(definition.pk),
        "text": (
            f"{definition.analyte.name} · {definition.method.code} "
            f"({definition.method.version}) · {definition.unit} · {definition.get_matrix_display()}"
        ),
        "matrix": definition.matrix,
        "matrix_label": definition.get_matrix_display(),
        "analyte": definition.analyte.name,
        "method_code": definition.method.code,
        "method_version": definition.method.version,
        "unit": definition.unit,
    }


class RequestedTestSelect(forms.SelectMultiple):
    def __init__(self, attrs=None):
        super().__init__({"data-requested-tests": "true", "size": 8, **(attrs or {})})

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if hasattr(value, "instance"):
            # Django escapes the attribute; UI reads metadata, never splits display labels.
            option["attrs"]["data-test"] = json.dumps(test_option_data(value.instance))
        return option


class RequestedTestsField(forms.ModelMultipleChoiceField):
    widget = RequestedTestSelect

    def label_from_instance(self, obj):
        return test_option_data(obj)["text"]
