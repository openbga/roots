import hashlib
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, Permission
from django.utils import timezone

from accounts.models import User
from analytical import services as analytical
from analytical.models import PreservationRule, Result
from catalog.models import Analyte, Method, TestDefinition
from core.models import Lab
from receipt.models import Client, Order, Project, RequestedTest, Sample
from reporting import services


def make_user(name, *roles, client=None, release=False):
    user = User.objects.create_user(
        username=name, email=f"{name}@example.invalid", password="x", client=client
    )
    for role in roles:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    if release:
        user.user_permissions.add(
            Permission.objects.get(codename="authorize_release", content_type__app_label="accounts")
        )
    return user


def fake_pdf(snapshot, *, preview=False):
    """Stand-in for WeasyPrint: bytes that identify the snapshot they were rendered from."""
    tag = snapshot["content_sha256"] + str(snapshot["release"]["version"])
    return b"%PDF-1.7 synthetic " + hashlib.sha256(tag.encode()).hexdigest().encode()


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture(autouse=True)
def stub_renderer(monkeypatch):
    monkeypatch.setattr(services, "render_pdf", fake_pdf)


@pytest.fixture
def people(db):
    return {
        "analyst": make_user("analyst1", "analyst"),
        "reviewer": make_user("reviewer1", "reviewer"),
        # Reviewer who also holds release authority: allowed to release.
        "approver": make_user("approver1", "reviewer", "approver", release=True),
        "approver2": make_user("approver2", "approver", release=True),
        "pm": make_user("pm1", "project_manager"),
        "custodian": make_user("custodian1", "custodian"),
    }


def make_sample(project, lab_id, definitions, creator, **extra):
    order = Order.objects.create(project=project, coc_reference=f"COC-{lab_id}", created_by=creator)
    now = timezone.now()
    matrix = extra.pop("matrix", "water")
    sample = Sample.objects.create(
        lab_id=lab_id,
        order=order,
        client_sample_id=f"CS-{lab_id}",
        matrix=matrix,
        collected_at=now - timedelta(days=2),
        received_at=now - timedelta(days=1),
        received_by=creator,
        collector_name="Synthetic Collector",
        collection_location="Test Creek outfall weir (synthetic)",
        cooler_temperature=Decimal("4.2"),
        cooler_temperature_original="4.2",
        seal_condition="Intact",
        preservation_check="Checked",
        current_location="Bench",
        **extra,
    )
    for definition in definitions:
        PreservationRule.objects.get_or_create(
            definition=definition,
            defaults={
                "temperature_mode": "not_required",
                "basis": "SYNTHETIC TEST: no temperature requirement; not method guidance.",
            },
        )
        RequestedTest.objects.create(sample=sample, definition=definition)
    return sample


def approve(sample, people, text="0.050", reason=""):
    """Enter, submit and approve every requested test of `sample` through analytical services."""
    method = sample.requested_tests.first().definition.method
    batch = analytical.create_batch(actor=people["analyst"], method=method, matrix="water")
    analytical.add_tests_to_batch(
        actor=people["analyst"],
        batch_id=batch.pk,
        requested_test_ids=[rt.pk for rt in sample.requested_tests.all()],
    )
    for result in Result.objects.filter(requested_test__sample=sample):
        revision = analytical.enter_result(
            actor=people["analyst"],
            result_id=result.pk,
            supplied_text=text,
            reporting_limit_applied="0.010",
            dilution_factor="1",
            analysis_started_at=timezone.now() - timedelta(hours=1),
            reason=reason,
        )
        analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
        analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)


@pytest.fixture
def world(people):
    return build_world(people)


def build_world(people):
    """Two clients of one lab, each with an approved sample and a draft report."""
    Lab.objects.create(name="Example Environmental Lab")
    method = Method.objects.create(code="DEMO-ICP", version="r1", title="Synthetic ICP demo")
    definition = TestDefinition.objects.create(
        method=method,
        analyte=Analyte.objects.create(name="Lead (synthetic)", cas_number="0000-00-0"),
        matrix="water",
        unit="mg/L",
        reporting_limit=Decimal("0.0100"),
    )
    client_a = Client.objects.create(name="Example Utilities")
    client_b = Client.objects.create(name="Sample Water District")
    project_a = Project.objects.create(client=client_a, name="Test Creek Outfall 001")
    project_b = Project.objects.create(client=client_b, name="Example Reservoir")
    sample_a = make_sample(project_a, "VR-A1", [definition], people["custodian"])
    sample_b = make_sample(project_b, "VR-B1", [definition], people["custodian"])
    approve(sample_a, people)
    approve(sample_b, people)
    report_a = services.create_report(
        actor=people["pm"], project=project_a, title="Outfall report", samples=[sample_a]
    )
    report_b = services.create_report(
        actor=people["pm"], project=project_b, title="Reservoir report", samples=[sample_b]
    )
    return {
        "definition": definition,
        "client_a": client_a,
        "client_b": client_b,
        "project_a": project_a,
        "sample_a": sample_a,
        "sample_b": sample_b,
        "report_a": report_a,
        "report_b": report_b,
        "user_a": make_user("client_a", client=client_a),
        "user_b": make_user("client_b", client=client_b),
    }


def accept_qc(reviewer, measurement, note):
    """Reviewer accepts the current QC finding for `measurement`, using analytical's own key."""
    from analytical.qc import BatchQC

    batch = measurement.qc_sample.batch
    (issue,) = [i for i in BatchQC.load(batch).all_issues() if i.measurement_id == measurement.pk]
    return analytical.record_corrective_action(
        actor=reviewer,
        batch_id=batch.pk,
        target_key=issue.key,
        disposition="accept_qualified",
        note=note,
    )
