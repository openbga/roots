"""CSV import: strict validation with clear row errors, all-or-nothing commit."""

import pytest
from django.core.exceptions import ValidationError

from analytical import services as svc
from analytical.importing import commit_import, decode_upload, parse_import
from analytical.models import ResultRevision
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db

HEADER = "sample_id,analyte,result,reporting_limit,dilution_factor,analysis_started_at,unit"
OK1 = "VR-DEMO-1,Lead (synthetic),0.0120,0.0050,1,2026-01-01T09:00-05:00,mg/L"


@pytest.fixture
def batch(config, analyst):
    b = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    tests = RequestedTest.objects.filter(sample=config["samples"][0])
    svc.add_tests_to_batch(actor=analyst, batch_id=b.pk, requested_test_ids=[t.pk for t in tests])
    return b


def csv_text(*rows, header=HEADER):
    return "\n".join([header, *rows]) + "\n"


def fix_times(text, config):
    """Replace the fixed demo date with a time safely after receipt and before now."""
    from datetime import timedelta

    from django.utils import timezone

    stamp = (timezone.now() - timedelta(hours=2)).replace(microsecond=0).isoformat()
    return text.replace("2026-01-01T09:00-05:00", stamp)


def test_valid_file_previews_cleanly_and_commits_exact_text(config, analyst, batch):
    nd_row = "VR-DEMO-1,Copper (synthetic),ND,0.0100,2,2026-01-01T09:00-05:00,mg/L"
    text = fix_times(csv_text(OK1, nd_row), config)
    preview = parse_import(batch, text, filename="run1.csv")
    assert preview.ok and len(preview.rows) == 2
    revisions = commit_import(actor=analyst, batch_id=batch.pk, text=text, filename="run1.csv")
    by = {r.analyte_name: r for r in revisions}
    lead = by["Lead (synthetic)"]
    assert lead.supplied_text == "0.0120" and lead.source == "import"
    assert by["Copper (synthetic)"].kind == "non_detect" and by["Copper (synthetic)"].value is None
    assert by["Lead (synthetic)"].source_detail == "run1.csv:row 2"


def test_errors_are_reported_per_row_and_nothing_is_written(config, analyst, batch):
    text = fix_times(
        csv_text(
            OK1,
            "VR-DEMO-1,Copper (synthetic),<0.01,0.0100,1,2026-01-01T09:00-05:00,mg/L",  # comparison
            "VR-DEMO-1,Lead (synthetic),0.2,0.0050,1,2026-01-01T09:00-05:00,mg/L",  # duplicate
            "VR-DEMO-9,Lead (synthetic),0.2,0.0050,1,2026-01-01T09:00-05:00,mg/L",  # unknown sample
        ),
        config,
    )
    preview = parse_import(batch, text)
    assert not preview.ok
    errors = {r.line: " ".join(r.errors) for r in preview.rows if r.errors}
    assert "Enter ND" in errors[3] and "Duplicate of row 2" in errors[4]
    assert "Add it to the batch" in errors[5] and 2 not in errors
    with pytest.raises(ValidationError):
        commit_import(actor=analyst, batch_id=batch.pk, text=text)
    assert not ResultRevision.objects.exists()


def test_unit_time_and_precision_errors(config, batch):
    text = fix_times(
        csv_text(
            "VR-DEMO-1,Lead (synthetic),0.2,0.0050,1,2026-01-01T09:00-05:00,ug/L",
            "VR-DEMO-1,Copper (synthetic),0.12345678901,0.0100,1,yesterday,mg/L",
        ),
        config,
    )
    errors = {r.line: " ".join(r.errors) for r in parse_import(batch, text).rows}
    assert "does not match" in errors[2] and "not converted" in errors[2]
    assert "ISO 8601" in errors[3] and "rounded" in errors[3]


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("", "empty"),
        (HEADER + "\n", "no data rows"),
        ("sample_id,analyte,result\nx,y,1\n", "Missing required column"),
        (HEADER + ",surprise\n" + OK1 + ",x\n", "Unknown column"),
        (HEADER.replace("unit", "result") + "\n", "Duplicate column"),
        ("a\x00b\n", "NUL"),
    ],
)
def test_file_level_errors(batch, text, fragment):
    preview = parse_import(batch, text)
    assert not preview.ok and fragment in " ".join(preview.file_errors)


def test_short_row_and_bad_encoding_and_size(batch):
    assert "Expected 7 columns" in parse_import(batch, csv_text("VR-DEMO-1,Lead")).rows[0].errors[0]
    with pytest.raises(ValidationError, match="UTF-8"):
        decode_upload(b"\xff\xfe\x00bad")
    with pytest.raises(ValidationError, match="larger"):
        decode_upload(b"x" * (1024 * 1024 + 1))
    assert decode_upload(b"\xef\xbb\xbfa,b").startswith("a")


def test_import_needs_analyst_and_existing_result_needs_reason(config, analyst, reviewer, batch):
    text = fix_times(csv_text(OK1), config)
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        commit_import(actor=reviewer, batch_id=batch.pk, text=text)
    commit_import(actor=analyst, batch_id=batch.pk, text=text)
    rev = ResultRevision.objects.get()
    svc.submit_result(actor=analyst, revision_id=rev.pk)
    assert "awaiting review" in parse_import(batch, text).rows[0].errors[0]


def test_strict_import_page_records_the_source_file_hash(config, client, analyst, batch):
    """The audit event of a strict import identifies the exact uploaded bytes, BOM and line
    endings included, as the mapped import does."""
    from django.urls import reverse

    from analytical.importing import encode_payload, file_digest
    from core.models import AuditEvent

    raw = b"\xef\xbb\xbf" + fix_times(csv_text(OK1), config).replace("\n", "\r\n").encode()
    client.force_login(analyst)
    commit = preview_page(client, batch, raw).context["commit_form"].initial
    assert commit["payload"] == encode_payload(raw)
    done = client.post(
        reverse("analytical:batch-import", args=[batch.pk]), commit | {"step": "commit"}
    )
    assert done.status_code == 302
    after = AuditEvent.objects.get(action="batch.results_imported").after
    assert after["file_sha256"] == file_digest(raw) and after["file_bytes"] == len(raw)
    assert after["filename"] == "run1.csv"


def preview_page(client, batch, raw, name="run1.csv"):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.urls import reverse

    return client.post(
        reverse("analytical:batch-import", args=[batch.pk]),
        {"step": "preview", "file": SimpleUploadedFile(name, raw)},
    )


def test_strict_commit_refuses_a_preview_made_stale_by_another_entry(config, analyst, batch):
    """Someone saved a draft for a targeted result after the preview: the commit must not
    supersede it unseen."""
    from analytical.importing import file_digest

    raw = fix_times(csv_text(OK1), config).encode()
    preview = parse_import(batch, decode_upload(raw), file_hash=file_digest(raw))
    assert preview.ok
    other = fix_times(csv_text(OK1.replace("0.0120", "0.0130")), config)
    commit_import(actor=analyst, batch_id=batch.pk, text=other)
    with pytest.raises(ValidationError, match="changed after the preview"):
        commit_import(actor=analyst, batch_id=batch.pk, data=raw, state_token=preview.state_token)
    assert ResultRevision.objects.get().supplied_text == "0.0130"


def test_strict_import_refuses_the_same_file_twice(config, analyst, batch):
    from analytical.importing import file_digest

    raw = fix_times(csv_text(OK1), config).encode()
    first = parse_import(batch, decode_upload(raw), file_hash=file_digest(raw))
    commit_import(actor=analyst, batch_id=batch.pk, data=raw, state_token=first.state_token)
    again = parse_import(batch, decode_upload(raw), file_hash=file_digest(raw))
    assert not again.ok and "already imported into this batch by analyst1" in again.file_errors[0]
    with pytest.raises(ValidationError, match="Nothing was imported"):
        commit_import(actor=analyst, batch_id=batch.pk, data=raw, state_token=again.state_token)
    assert ResultRevision.objects.count() == 1
    # A changed file is a new import, and a mapped import of these bytes is judged separately.
    edited = raw.replace(b"0.0120", b"0.0125")
    assert parse_import(batch, decode_upload(edited), file_hash=file_digest(edited)).ok


def test_strict_import_page_refuses_a_repeated_commit(config, client, analyst, batch):
    """The confirm button pressed twice (or the page resubmitted) imports the file once."""
    from django.urls import reverse

    raw = fix_times(csv_text(OK1), config).encode()
    client.force_login(analyst)
    commit = preview_page(client, batch, raw).context["commit_form"].initial | {"step": "commit"}
    url = reverse("analytical:batch-import", args=[batch.pk])
    assert client.post(url, commit).status_code == 302
    again = client.post(url, commit)
    assert again.status_code == 200
    assert b"already imported into this batch" in again.content
    assert b'name="step" value="commit"' not in again.content
    assert ResultRevision.objects.count() == 1
    missing = {k: v for k, v in commit.items() if k != "state_token"}
    assert client.post(url, missing).status_code == 200
    assert ResultRevision.objects.count() == 1
