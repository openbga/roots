from datetime import datetime

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from analytical.parsing import parse_timestamp
from analytical.services import lab_timezone
from core.models import Lab
from receipt.forms import ReceiptForm, TransferForm


@pytest.mark.django_db
@override_settings(TIME_ZONE="America/New_York")
@pytest.mark.parametrize(
    "supplied, expected",
    [
        ("2026-01-15T14:30", "2026-01-15T19:30+00:00"),
        ("2026-07-15T14:30", "2026-07-15T18:30+00:00"),
        ("2026-01-15T14:30+09:00", "2026-01-15T05:30+00:00"),
    ],
)
def test_receipt_custody_and_csv_use_deployment_zone_despite_stale_lab_copy(supplied, expected):
    lab = Lab.objects.create(name="Synthetic lab", timezone="Pacific/Honolulu")
    with timezone.override(None):
        receipt = ReceiptForm().fields["collected_at"].clean(supplied)
        custody = TransferForm().fields["occurred_at"].clean(supplied)
        imported = parse_timestamp(supplied, lab_timezone())
    assert receipt == custody == imported == datetime.fromisoformat(expected)
    lab.refresh_from_db()
    assert lab.timezone == "Pacific/Honolulu"  # a parsing read never rewrites setup history


@pytest.mark.django_db
@override_settings(TIME_ZONE="America/New_York")
@pytest.mark.parametrize("supplied", ["2026-03-08T02:30", "2026-11-01T01:30"])
def test_forms_and_csv_reject_nonexistent_or_ambiguous_naive_times(supplied):
    Lab.objects.create(name="Synthetic lab", timezone="UTC")
    with timezone.override(None):
        with pytest.raises(ValidationError):
            ReceiptForm().fields["collected_at"].clean(supplied)
        with pytest.raises(ValidationError):
            parse_timestamp(supplied, lab_timezone())
