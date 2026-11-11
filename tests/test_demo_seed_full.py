"""`seed_demo --full`: a synthetic working week made through the domain services, once."""

from collections import Counter

import pytest
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.models import User
from analytical import charts
from analytical.models import (
    Batch,
    MDLCalculation,
    MDLObservation,
    QCCorrectiveAction,
    Result,
    ResultRevision,
)
from analytical.qc import BatchQC
from catalog.models import TestDefinition
from commercial.models import Quote, Submission
from core.models import AuditEvent, Lab
from laboratory.models import Material, Nonconformance
from receipt.models import Client, RequestedTest, Sample
from reporting.models import Report, ReportVersion

pytestmark = pytest.mark.django_db


@pytest.fixture
def password(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-test-password-only")


def row_counts():
    return {
        model._meta.label: model.objects.count()
        for model in apps.get_models()
        if not model._meta.proxy and model._meta.managed
    }


def passwords():
    return dict(User.objects.values_list("username", "password"))


def lead_water():
    return TestDefinition.objects.get(
        method__code="DEMO-METALS-AQ",
        method__version="demo-1",
        matrix="water",
        analyte__name="Lead",
    )


def test_full_seed_builds_a_working_week_once(password, monkeypatch):
    call_command("seed_demo", full=True, verbosity=0)

    # Two clients, a few dozen samples over three matrices, with hold and reject at receipt.
    assert set(Client.objects.values_list("name", flat=True)) == {
        "Example Utilities",
        "Example County Sewer District",
    }
    assert Sample.objects.count() >= 40
    assert set(Sample.objects.values_list("matrix", flat=True)) == {"water", "wastewater", "soil"}
    dispositions = Counter(Sample.objects.values_list("disposition", flat=True))
    assert dispositions["hold"] >= 1 and dispositions["reject"] >= 1
    assert not Sample.objects.exclude(client_sample_id__regex=r"^[A-Z0-9-]+$").exists()

    # Every stage of the work is present: just received, in analysis, under review, done.
    assert RequestedTest.objects.filter(result__isnull=True).count() >= 5
    statuses = Counter(Result.objects.values_list("status", flat=True))
    for status in ("awaiting", "draft", "returned", "submitted"):
        assert statuses[status] >= 1, status
    assert statuses["approved"] >= 30
    assert all(
        r.reviewed_by_id not in (r.entered_by_id, r.submitted_by_id)
        for r in ResultRevision.objects.filter(status="approved")
    )

    # A batch awaiting review with passing QC, and one with a QC failure and no disposition.
    review = list(Batch.objects.filter(results__status="submitted").distinct())
    blocked = [b for b in review if any(i.blocking for i in BatchQC.load(b).all_issues())]
    assert blocked and len(blocked) < len(review)
    assert not QCCorrectiveAction.objects.exists()
    assert Batch.objects.filter(kind="preparation").exists()

    # Control chart history with exactly one point outside the configured LCS limits.
    series = charts.build_series(lead_water(), "lcs", "recovery")
    assert series.counts["points"] >= 20
    assert series.counts["outside_configured"] == 1

    # An MDL study with its spikes and imported routine blanks, calculated, awaiting review.
    assert MDLObservation.objects.filter(kind="spike").count() == 7
    assert MDLObservation.objects.filter(kind="blank").count() >= 7
    calculation = MDLCalculation.objects.get()
    assert calculation.status == "calculated" and not calculation.decision

    # Reports: drafts, a released first version and an amendment, visible to the portal client.
    versions = Counter(Report.objects.values_list("latest_version", flat=True))
    assert versions[0] >= 1 and versions[1] >= 1 and versions[2] == 1
    amendment = ReportVersion.objects.get(number=2)
    assert amendment.amends.number == 1 and amendment.amendment_reason.startswith("Synthetic")
    assert bytes(amendment.file.pdf).startswith(b"%PDF")
    approver = User.objects.get(username="demo_approver")
    assert set(ReportVersion.objects.values_list("released_by", flat=True)) == {approver.pk}
    client = User.objects.get(username="demo_client").client
    assert ReportVersion.objects.filter(report__project__client=client).count() == 3

    # The commercial and laboratory demo records come along.
    assert Quote.objects.count() == 3 and Submission.objects.count() == 3
    assert Material.objects.filter(name__startswith="Demo ").exists()
    assert Nonconformance.objects.exists()

    # Everything went through the services: attributed audit events, all synthetic accounts.
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"sample.received", "result.approved", "report.released", "report.amended"} <= actions
    assert not User.objects.exclude(email__endswith="@example.invalid").exists()

    # A second run, even with another password, adds and changes nothing.
    before, hashes = row_counts(), passwords()
    monkeypatch.setenv("DEMO_PASSWORD", "another-synthetic-password")
    call_command("seed_demo", full=True, verbosity=0)
    assert row_counts() == before
    assert passwords() == hashes


def test_full_seed_refuses_a_non_demo_laboratory(password):
    Lab.objects.create(pk=1, name="Riverside Water Laboratory")
    before = row_counts()
    with pytest.raises(CommandError, match="non-demo laboratory"):
        call_command("seed_demo", full=True, verbosity=0)
    assert row_counts() == before


def test_full_seed_without_release_authority_saves_nothing(password):
    call_command("seed_demo", verbosity=0)
    User.objects.get(username="demo_approver").user_permissions.clear()
    before = row_counts()
    with pytest.raises(CommandError, match="release permission"):
        call_command("seed_demo", full=True, verbosity=0)
    assert row_counts() == before
    assert not User.objects.get(username="demo_approver").user_permissions.exists()


def test_default_seed_stays_minimal(password):
    call_command("seed_demo", verbosity=0)
    assert Client.objects.count() == 1
    assert Sample.objects.count() == 1
    assert not Batch.objects.exists()
    assert not Report.objects.exists()
    assert not Quote.objects.exists()
