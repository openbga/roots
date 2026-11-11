"""settings.TIME_ZONE is the sole authority for new naive input; importer and forms agree."""

from datetime import timedelta
from datetime import timezone as fixed_tz
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from analytical import services as svc
from analytical.forms import local_datetime
from analytical.importing import parse_import
from analytical.parsing import parse_timestamp
from core.models import Lab
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db


def test_naive_times_follow_settings_not_lab_timezone(settings, config, analyst):
    settings.TIME_ZONE = "America/Chicago"
    Lab.objects.update_or_create(
        pk=1, defaults={"name": "Synthetic Lab", "timezone": "Pacific/Auckland"}
    )  # mismatched
    assert svc.lab_timezone() == "America/Chicago"

    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    tests = RequestedTest.objects.filter(sample=config["samples"][0])
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[t.pk for t in tests]
    )
    instant = (timezone.now() - timedelta(hours=2)).replace(microsecond=0)
    naive = instant.astimezone(ZoneInfo("America/Chicago")).replace(tzinfo=None).isoformat()
    aware = instant.astimezone(fixed_tz(timedelta(hours=5))).isoformat()  # +05:00, same instant
    header = "sample_id,analyte,result,reporting_limit,dilution_factor,analysis_started_at"
    text = (
        f"{header}\nVR-DEMO-1,Lead (synthetic),0.0123,0.005,1,{naive}\n"
        f"VR-DEMO-1,Copper (synthetic),0.0456,0.010,1,{aware}\n"
    )
    preview = parse_import(batch, text)
    assert preview.ok, [r.errors for r in preview.rows]
    naive_row, aware_row = (r.entry["analysis_started_at"] for r in preview.rows)
    assert naive_row == instant  # read in settings.TIME_ZONE, not Pacific/Auckland
    assert aware_row == instant and aware_row.utcoffset() == timedelta(hours=5)  # offset kept

    # the form field parses the same text to the same instant as the importer
    assert local_datetime("Analysis start").clean(naive) == naive_row


def test_ambiguous_and_nonexistent_naive_times_are_still_rejected(settings):
    settings.TIME_ZONE = "America/Chicago"
    zone = svc.lab_timezone()
    for text in ("2026-11-01T01:30", "2026-03-08T02:30"):
        with pytest.raises(ValidationError, match="ambiguous or does not exist"):
            parse_timestamp(text, zone)
    assert parse_timestamp("2026-11-01T01:30-05:00", zone).utcoffset() == timedelta(hours=-5)
