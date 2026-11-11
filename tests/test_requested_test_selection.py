import json

import pytest

from catalog.models import TestDefinition
from receipt.forms import OrderSampleForm


@pytest.mark.django_db
def test_matrix_change_keeps_selections_and_identifies_incompatible_tests(receipt_data):
    water = receipt_data["tests"][0]
    soil = TestDefinition.objects.create(
        method=water.method, analyte=water.analyte, matrix="soil", unit="mg/kg"
    )
    payload = {
        **receipt_data,
        "expected_revision": "1",
        "cooler_temperature": str(receipt_data["cooler_temperature"]),
        "coolant": "wet_ice",
        "matrix": "soil",
        "tests": [str(water.pk), str(soil.pk)],
    }
    form = OrderSampleForm(payload)
    assert not form.is_valid()
    assert set(form.errors) == {"tests"}
    assert "do not match Soil" in form.errors["tests"][0]
    assert "Remove them or correct" in form.errors["tests"][0]
    assert form["tests"].value() == [str(water.pk), str(soil.pk)]
    selected = [
        option
        for _, options, _ in form.fields["tests"].widget.optgroups("tests", form["tests"].value())
        for option in options
        if option["selected"]
    ]
    metadata = {
        str(option["value"]): json.loads(option["attrs"]["data-test"]) for option in selected
    }
    assert metadata[str(water.pk)]["matrix"] == "water"
    assert metadata[str(soil.pk)] == {
        "value": str(soil.pk),
        "text": (
            f"{water.analyte.name} · {water.method.code} ({water.method.version}) · mg/kg · Soil"
        ),
        "matrix": "soil",
        "matrix_label": "Soil",
        "analyte": water.analyte.name,
        "method_code": water.method.code,
        "method_version": water.method.version,
        "unit": "mg/kg",
    }
    corrected = OrderSampleForm({**payload, "tests": [str(soil.pk)]})
    assert corrected.is_valid(), corrected.errors
    soil.is_active = False
    soil.save(update_fields=["is_active"])
    inactive = OrderSampleForm({**payload, "tests": [str(soil.pk)]})
    assert not inactive.is_valid() and "tests" in inactive.errors
