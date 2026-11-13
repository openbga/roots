"""Client address and sample designation on reports: shown when recorded, absent otherwise."""

import pytest

from receipt.models import Sample
from reporting.content import assemble, describe_changes, fingerprint
from reporting.pdf import render_html

from .conftest import texts

pytestmark = pytest.mark.django_db


def test_content_adds_recorded_intake_fields_without_changing_older_content(world, people):
    report = world["report_a"]
    before = assemble(report, actor=people["approver"]).content
    sample = before["samples"][0]
    assert "address" not in before["client"]
    assert not {"source", "sample_type", "coolant"} & set(sample)

    client = world["client_a"]
    client.address_line1, client.city, client.state = "100 Example Street", "Exampleville", "NY"
    client.postal_code = "00000"
    client.save()
    Sample.objects.filter(pk=world["sample_a"].pk).update(
        source_type="groundwater", sample_type="trip_blank", coolant="wet_ice"
    )
    after = assemble(report, actor=people["approver"]).content
    assert after["client"]["address"] == ["100 Example Street", "Exampleville, NY 00000"]
    assert {k: after["samples"][0][k] for k in ("source", "sample_type", "coolant")} == {
        "source": "Groundwater",
        "sample_type": "Trip blank",
        "coolant": "Wet ice",
    }
    assert fingerprint(before) != fingerprint(after)
    changes = texts(describe_changes(before, after))
    assert "Client address changed." in changes
    assert any("sample type" in change and "VR-A1" in change for change in changes)

    html = render_html({"content": after, "content_sha256": fingerprint(after), "release": None})
    assert "100 Example Street" in html and "Trip blank" in html and "groundwater" in html
    assert "coolant on arrival: wet ice" in html
    # A snapshot issued before these fields existed still renders.
    old = render_html({"content": before, "content_sha256": fingerprint(before), "release": None})
    assert "Trip blank" not in old and "coolant on arrival" not in old
