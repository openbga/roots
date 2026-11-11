"""Server-rendered parts of the visual language (docs/ui-standards.md section 12).

The dateline and the record foot line print dates in the laboratory's time zone (TIME_ZONE), never
the browser's; the record foot line also marks a record page (numbered panels); history strikes
through the prior value of a change.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.template.loader import render_to_string
from django.test import override_settings
from django.urls import reverse

from receipt.services import receive_sample

# Fourteen hours ahead of and eleven behind UTC: the local dates always differ.
FAR_EAST, FAR_WEST = "Pacific/Kiritimati", "Pacific/Pago_Pago"


def today(zone):
    return datetime.now(ZoneInfo(zone)).strftime("%Y-%m-%d")


@pytest.mark.parametrize("zone", [FAR_EAST, FAR_WEST])
def test_dateline_and_record_foot_use_the_laboratory_time_zone(zone, staff):
    viewer = staff("theme-viewer", "custodian")
    with override_settings(TIME_ZONE=zone):
        dateline = render_to_string("includes/dateline.html")
        foot = render_to_string(
            "includes/record_foot.html", {"identifier": "VR-000001", "user": viewer}
        )
    assert f'<time datetime="{today(zone)}">' in dateline
    assert "Week " in dateline
    assert 'class="vr-record-foot"' in foot
    assert "VR-000001" in foot
    assert f'Viewed <time datetime="{today(zone)}T' in foot
    assert "by theme-viewer" in foot


def test_history_strikes_through_the_prior_value():
    html = render_to_string(
        "includes/history_item.html",
        {"title": "Corrected", "changes": [("Matrix", "Water", "Wastewater"), ("Note", "", "x")]},
    )
    assert '<del class="vr-prior">Water</del>' in html
    assert '<del class="vr-prior">blank</del>' in html
    assert "changed to</span> Wastewater" in html


@pytest.mark.django_db
def test_record_page_has_foot_line_and_dashboard_has_dateline(
    client, staff, receipt_data, settings
):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    custodian = staff("theme-custodian", "custodian")
    sample = receive_sample(actor=custodian, **receipt_data)
    client.force_login(custodian)

    record = client.get(reverse("receipt:sample-detail", args=[sample.pk])).content.decode()
    assert '<footer class="vr-record-foot"><span class="vr-id">' + sample.lab_id in record
    assert record.count('class="vr-record-foot"') == 1

    dashboard = client.get(reverse("dashboard")).content.decode()
    assert 'class="vr-dateline"' in dashboard
    assert 'class="vr-record-foot"' not in dashboard
