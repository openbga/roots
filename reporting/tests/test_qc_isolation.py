"""A batch shared by two clients must not disclose one client's data on the other's report."""

import json
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from analytical import services as analytical
from analytical.models import QCLimit, QCMeasurement, Result
from reporting.content import assemble
from reporting.pdf import render_html
from reporting.services import create_report

from .conftest import accept_qc, make_sample

# Values and free text that belong to client B's sample only.
B_SECRETS = ['"0.4700000000"', '"0.60"', "VR-B2", "MS-VR-B2", "Matrix interference"]
# Client A's own client-derived QC: shown on A's report, never on B's.
A_OWN = ["DUP-VR-A2", '"0.051"']


def shared_batch(world, people):
    definition, an, rv = world["definition"], people["analyst"], people["reviewer"]
    for qc_type, measure, lower, upper in (
        ("lcs", "recovery", "80", "120"),
        ("matrix_spike", "recovery", "80", "120"),
        ("lab_duplicate", "rpd", None, "20"),
    ):
        QCLimit.objects.create(
            definition=definition,
            qc_type=qc_type,
            measure=measure,
            lower=Decimal(lower) if lower else None,
            upper=Decimal(upper),
        )
    a2 = make_sample(world["project_a"], "VR-A2", [definition], people["custodian"])
    b2 = make_sample(world["client_b"].projects.get(), "VR-B2", [definition], people["custodian"])
    batch = analytical.create_batch(actor=an, method=definition.method, matrix="water")
    analytical.add_tests_to_batch(
        actor=an,
        batch_id=batch.pk,
        requested_test_ids=[a2.requested_tests.get().pk, b2.requested_tests.get().pk],
    )
    at = timezone.now() - timedelta(hours=1)

    def qc(qc_type, label, text, parent=None, **values):
        sample = analytical.add_qc_sample(
            actor=an, batch_id=batch.pk, qc_type=qc_type, label=label, parent_sample=parent
        )
        analytical.record_qc_measurement(
            actor=an,
            qc_sample_id=sample.pk,
            definition_id=definition.pk,
            observed_text=text,
            reporting_limit_applied="0.010",
            analysis_started_at=at,
            **values,
        )
        return sample

    qc("lcs", "LCS-77", "0.98", spike_value="1.00")
    ms = qc("matrix_spike", "MS-VR-B2", "0.60", b2, spike_value="0.50", native_value="0.47")
    qc("lab_duplicate", "DUP-VR-A2", "0.051", a2, reference_value="0.050")
    failing = QCMeasurement.objects.get(qc_sample=ms, is_current=True)
    accept_qc(rv, failing, "Matrix interference in VR-B2 (native 0.47 mg/L).")
    for sample, text in ((a2, "0.050"), (b2, "0.470")):
        result = Result.objects.get(requested_test__sample=sample)
        revision = analytical.enter_result(
            actor=an,
            result_id=result.pk,
            supplied_text=text,
            reporting_limit_applied="0.010",
            dilution_factor="1",
            analysis_started_at=at,
        )
        analytical.submit_result(actor=an, revision_id=revision.pk)
        analytical.approve_result(actor=rv, revision_id=revision.pk)
    report_a = create_report(
        actor=people["pm"], project=world["project_a"], title="Shared batch A", samples=[a2]
    )
    report_b = create_report(
        actor=people["pm"],
        project=world["client_b"].projects.get(),
        title="Shared batch B",
        samples=[b2],
    )
    return report_a, report_b


def qc_of(content):
    (batch,) = content["batches"]
    (evidence,) = batch["qc"].values()
    return evidence


def test_other_clients_qc_source_data_is_not_disclosed(world, people):
    report_a, report_b = shared_batch(world, people)
    a = assemble(report_a)
    assert a.releasable, a.blockers
    text = json.dumps(a.content)
    html = render_html({"content": a.content, "release": None}, preview=True)
    for secret in B_SECRETS:
        assert secret not in text, secret
        assert secret.strip('"') not in html, secret
    for own in A_OWN:
        assert own in text, own  # A's own duplicate evidence stays visible to A

    evidence = qc_of(a.content)
    by_type = {m["qc_type"]: m for m in evidence["measurements"]}
    spike = by_type["matrix_spike"]
    assert spike["observed_text"] is None and spike["native_value"] is None
    assert spike["withheld"] is True and spike["evaluation"]["status"] == "fail"
    assert by_type["lcs"]["observed_text"] == "0.98"  # lab control evidence unchanged
    (issue,) = evidence["issues"]
    assert issue["disposition"] == "accept_qualified" and issue["resolved"]
    assert issue["note"] == "" and "VR-B2" not in issue["message"]

    b = json.dumps(assemble(report_b).content)
    for own in A_OWN:
        assert own not in b, own
    assert '"0.4700000000"' in b and "MS-VR-B2" in b  # B sees its own spike's source data
