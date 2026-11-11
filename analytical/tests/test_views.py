"""Smoke journey through the pages: worklist to approval, plus access control."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from analytical import services as svc
from analytical.importing import encode_payload
from analytical.models import Batch, Result, ResultRevision
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db


def stamp(hours=1):
    return (timezone.localtime() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def test_page_journey_from_worklist_to_approval(config, client, analyst, reviewer):
    from receipt.models import Sample  # noqa: F401

    client.force_login(analyst)
    page = client.get(reverse("analytical:batch-worklist"))
    assert page.status_code == 200 and b"VR-DEMO-1" in page.content
    assert b" left</div>" in page.content  # collection clock within its limit

    created = client.post(
        reverse("analytical:batch-create"),
        {"method": config["method"].pk, "matrix": "water", "prepared_at": "", "notes": ""},
    )
    batch = Batch.objects.get()
    assert created.status_code == 302 and created.url.endswith(f"/batches/{batch.pk}/tests/")
    tests = RequestedTest.objects.filter(sample=config["samples"][0])
    ids = list(tests.values_list("pk", flat=True))
    client.post(reverse("analytical:batch-add-tests", args=[batch.pk]), {"requested_tests": ids})
    assert Result.objects.filter(batch=batch).count() == 2

    for qc_type, label in (("method_blank", "MB-1"), ("lcs", "LCS-1")):
        r = client.post(
            reverse("analytical:qc-sample-add", args=[batch.pk]),
            {"qc_type": qc_type, "label": label},
        )
        assert r.status_code == 302
    for sample in batch.qc_samples.all():
        url = reverse("analytical:qc-measure", args=[sample.pk])
        assert client.get(url).status_code == 200
        defs = config["defs"].values()
        data = {
            "form-TOTAL_FORMS": 2,
            "form-INITIAL_FORMS": 2,
            "form-MIN_NUM_FORMS": 0,
            "form-MAX_NUM_FORMS": 1000,
            "analysis_started_at": stamp(),
            "reason": "",
        }
        for i, d in enumerate(sorted(defs, key=lambda d: d.analyte.name)):
            data |= {
                f"form-{i}-definition_id": d.pk,
                f"form-{i}-reporting_limit_applied": str(d.reporting_limit),
                f"form-{i}-observed_text": "ND" if sample.qc_type == "method_blank" else "10",
            }
            if sample.qc_type == "lcs":
                data[f"form-{i}-spike_value"] = "10"
        assert client.post(url, data).status_code == 302

    entry_url = reverse("analytical:batch-entry", args=[batch.pk])
    grid = client.get(entry_url)
    assert grid.status_code == 200 and b"Result entry grid" in grid.content
    results = {
        r.requested_test.definition.analyte.name: r for r in Result.objects.filter(batch=batch)
    }
    data = {
        "form-TOTAL_FORMS": 2,
        "form-INITIAL_FORMS": 2,
        "form-MIN_NUM_FORMS": 0,
        "form-MAX_NUM_FORMS": 1000,
        "default_analysis_started_at": stamp(),
    }
    for i, name in enumerate(sorted(results)):
        data |= {
            f"form-{i}-result_id": results[name].pk,
            f"form-{i}-supplied_text": "0.0300",
            f"form-{i}-reporting_limit_applied": "0.0100",
            f"form-{i}-dilution_factor": "1",
        }
    # a bad value shows an inline error and saves nothing
    bad = client.post(entry_url, {**data, "form-0-supplied_text": "<0.01"})
    assert bad.status_code == 200 and b"Enter ND" in bad.content
    assert not ResultRevision.objects.exists()
    assert client.post(entry_url, data).status_code == 302
    revs = list(ResultRevision.objects.values_list("pk", flat=True))
    assert len(revs) == 2

    assert (
        client.post(
            reverse("analytical:batch-submit", args=[batch.pk]), {"revisions": revs}
        ).status_code
        == 302
    )
    detail = client.get(reverse("analytical:batch-detail", args=[batch.pk]))
    assert detail.status_code == 200 and b"Submitted for review" in detail.content

    # analysts cannot see approve controls and cannot approve
    review = client.get(reverse("analytical:review-batch", args=[batch.pk]))
    assert review.status_code == 200 and b"Your work" in review.content
    denied = client.post(
        reverse("analytical:review-batch", args=[batch.pk]),
        {"revisions": revs, "action": "approve", "note": ""},
    )
    assert denied.status_code == 403

    client.force_login(reviewer)
    assert batch.number.encode() in client.get(reverse("analytical:review-list")).content
    client.post(
        reverse("analytical:review-batch", args=[batch.pk]),
        {"revisions": revs, "action": "return", "note": ""},
    )  # reason required
    assert ResultRevision.objects.filter(status="submitted").count() == 2
    client.post(
        reverse("analytical:review-batch", args=[batch.pk]),
        {"revisions": revs, "action": "approve", "note": "ok"},
    )
    assert ResultRevision.objects.filter(status="approved").count() == 2
    assert (
        client.get(
            reverse("analytical:result-detail", args=[results["Lead (synthetic)"].pk])
        ).status_code
        == 200
    )


def test_access_control(config, client, custodian_user, reviewer, analyst):
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    from accounts.models import User
    from receipt.models import Client as ClientRecord

    portal = User.objects.create_user(
        "portal", "p@example.invalid", "x", client=ClientRecord.objects.first()
    )
    for user in (portal,):
        client.force_login(user)
        for name in (
            "analytical:batch-worklist",
            "analytical:batch-list",
            "analytical:review-list",
            "analytical:qc-config",
        ):
            assert client.get(reverse(name)).status_code == 403
        assert client.get(reverse("analytical:batch-detail", args=[batch.pk])).status_code == 403
    client.force_login(reviewer)  # read access for staff, writes refused
    assert client.get(reverse("analytical:batch-detail", args=[batch.pk])).status_code == 200
    assert client.get(reverse("analytical:batch-create")).status_code == 403
    client.logout()
    assert client.get(reverse("analytical:batch-list")).status_code == 302


def test_config_page_and_edit_needs_admin(config, client, admin_user, analyst):
    from analytical.models import QCLimit

    limit = QCLimit.objects.first()
    client.force_login(analyst)
    assert client.get(reverse("analytical:qc-config")).status_code == 200
    assert (
        client.post(reverse("analytical:config-edit", args=["qc-limit", limit.pk]), {}).status_code
        == 403
    )
    client.force_login(admin_user)
    page = client.get(reverse("analytical:config-edit", args=["qc-limit", limit.pk]))
    assert page.status_code == 200
    data = {
        "original_state": page.context["form"]["original_state"].value(),
        "definition": limit.definition_id,
        "phase": limit.phase,
        "qc_type": limit.qc_type,
        "measure": limit.measure,
        "lower": "75",
        "upper": "125",
        "basis": "SOP",
        "is_active": "on",
        "reason": "",
    }
    missing_reason = client.post(page.request["PATH_INFO"], data)
    assert missing_reason.status_code == 200 and b"Give the reason" in missing_reason.content
    ok = client.post(page.request["PATH_INFO"], {**data, "reason": "SOP revision"})
    assert ok.status_code == 302
    limit.refresh_from_db()
    assert str(limit.lower).startswith("75")


def test_import_pages_preview_then_commit(config, client, analyst):
    from django.core.files.uploadedfile import SimpleUploadedFile

    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    tests = RequestedTest.objects.filter(sample=config["samples"][0])
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[t.pk for t in tests]
    )
    client.force_login(analyst)
    url = reverse("analytical:batch-import", args=[batch.pk])
    assert client.get(url).status_code == 200
    when = (timezone.now() - timedelta(hours=2)).isoformat()
    header = "sample_id,analyte,result,reporting_limit,dilution_factor,analysis_started_at"
    bad = f"{header}\nVR-DEMO-1,Lead (synthetic),<5,0.005,1,{when}\n"
    page = client.post(url, {"step": "preview", "file": SimpleUploadedFile("r.csv", bad.encode())})
    assert page.status_code == 200 and b"Enter ND" in page.content
    assert b'name="step" value="commit"' not in page.content  # cannot confirm an invalid file
    good = f"{header}\nVR-DEMO-1,Lead (synthetic),0.0123,0.005,1,{when}\n"
    page = client.post(url, {"step": "preview", "file": SimpleUploadedFile("r.csv", good.encode())})
    assert b'name="step" value="commit"' in page.content and not ResultRevision.objects.exists()
    commit = page.context["commit_form"].initial
    assert commit["payload"] == encode_payload(good.encode()) and commit["filename"] == "r.csv"
    done = client.post(url, commit | {"step": "commit"})
    assert done.status_code == 302
    assert ResultRevision.objects.get().supplied_text == "0.0123"


def test_qc_disposition_page_requires_reviewer_for_acceptance(config, client, analyst, reviewer):
    from analytical.qc import BatchQC

    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    tests = RequestedTest.objects.filter(sample=config["samples"][0])
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[t.pk for t in tests]
    )
    key = BatchQC.load(batch).issues(config["defs"]["Lead"])[0].key  # shortfall of blank
    url = reverse("analytical:qc-action", args=[batch.pk])
    client.force_login(analyst)
    assert client.get(url, {"target": key}).status_code == 200
    assert client.get(url, {"target": "m:999"}).status_code == 404
    assert (
        client.post(
            url, {"target_key": key, "disposition": "accept_qualified", "note": "x"}
        ).status_code
        == 403
    )
    client.force_login(reviewer)
    assert (
        client.post(
            url, {"target_key": key, "disposition": "accept_qualified", "note": "Documented"}
        ).status_code
        == 302
    )


def test_strict_import_commit_reports_a_damaged_payload(config, client, analyst):
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    client.force_login(analyst)
    url = reverse("analytical:batch-import", args=[batch.pk])
    done = client.post(url, {"step": "commit", "filename": "r.csv", "payload": "not base64 !!"})
    assert done.status_code == 200 and b"could not be read back" in done.content
    assert not ResultRevision.objects.exists()
