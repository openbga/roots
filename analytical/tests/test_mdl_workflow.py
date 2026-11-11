"""MDL study workflow on PostgreSQL: calculation, independent review, not-evaluated handling."""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from analytical import mdl_services as svc
from analytical import services as base
from analytical.models import MDLCalculation
from core.models import AuditEvent

from .test_workflow import start, when

pytestmark = pytest.mark.django_db
D = Decimal
SPIKES = ("1", "2", "3", "4", "5", "6", "7")
BLANKS = ("0.1", "0.2", "0.1", "0.3", "0.2", "0.1", "0.2")


def study_for(config, actor, *, kind="initial", level="5"):
    return svc.create_study(
        actor=actor, definition=config["defs"]["Lead"], kind=kind, spiking_level=level
    )


def fill(study, actor, spikes=SPIKES, blanks=BLANKS, *, start_day=10):
    now = timezone.now()
    for i, value in enumerate(spikes):
        at = now - timedelta(days=start_day + i)
        svc.add_observation(
            actor=actor,
            study_id=study.pk,
            kind="spike",
            supplied_text=value,
            spike_level=str(study.spiking_level),
            qualitative_ok=True,
            prepared_at=at - timedelta(hours=1),
            analyzed_at=at,
            batch_label=f"S{i % 3}",
            preparation_label=f"P{i}",
        )
    for i, value in enumerate(blanks):
        at = now - timedelta(days=start_day + i)
        svc.add_observation(
            actor=actor,
            study_id=study.pk,
            kind="blank",
            supplied_text=value,
            prepared_at=at - timedelta(hours=1),
            analyzed_at=at,
            batch_label=f"S{i % 3}",
            preparation_label=f"P{i}",
        )


@pytest.mark.parametrize("actor_fixture", ["both", "admin_user"])
def test_full_path_calculates_from_exact_decimals_and_needs_an_independent_review(
    config, analyst, reviewer, request, actor_fixture
):
    both = request.getfixturevalue(actor_fixture)
    study = study_for(config, analyst)
    fill(study, analyst)
    calc = svc.calculate(actor=both, study_id=study.pk)  # run by a person who can also review
    sd = (sum((D(v) - 4) ** 2 for v in SPIKES) / 6).sqrt()
    assert calc.status == "calculated" and calc.reasons == []
    assert calc.mdl_s == (D("3.143") * sd).quantize(D(1).scaleb(-10), rounding=ROUND_HALF_UP)
    assert calc.verified_mdl >= calc.mdl_s and calc.unit == "mg/L"
    assert len(calc.inputs) == 14 and all(row["used"] for row in calc.inputs)
    assert calc.inputs[0]["supplied_text"] in SPIKES + BLANKS  # original text preserved
    with pytest.raises(PermissionDenied):  # the person who ran it cannot approve it
        svc.review_calculation(actor=both, calculation_id=calc.pk, decision="adopted")
    with pytest.raises(PermissionDenied):  # nor can a reviewer who entered the data
        mixed = study_for(config, both)
        fill(mixed, both)
        other = svc.calculate(actor=analyst, study_id=mixed.pk)
        svc.review_calculation(actor=both, calculation_id=other.pk, decision="adopted")
    done = svc.review_calculation(actor=reviewer, calculation_id=calc.pk, decision="adopted")
    assert done.adopted_mdl == calc.verified_mdl
    assert svc.current_calculation(config["defs"]["Lead"].pk).pk == calc.pk
    with pytest.raises(ValidationError, match="already been reviewed"):
        svc.review_calculation(actor=reviewer, calculation_id=calc.pk, decision="adopted")
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {
        "mdl.study_created",
        "mdl.observation_added",
        "mdl.calculated",
        "mdl.reviewed",
    } <= actions


def test_insufficient_data_is_not_evaluated_with_reasons_and_cannot_be_adopted(
    config, analyst, reviewer
):
    study = study_for(config, analyst)
    fill(study, analyst, spikes=SPIKES[:5], blanks=BLANKS[:3])
    calc = svc.calculate(actor=analyst, study_id=study.pk)
    assert calc.status == "not_evaluated" and calc.verified_mdl is None and calc.mdl_s is None
    assert any("at least 7" in r for r in calc.reasons) and len(calc.reasons) >= 2
    with pytest.raises(ValidationError, match="not evaluated"):
        svc.review_calculation(actor=reviewer, calculation_id=calc.pk, decision="adopted")
    assert svc.current_calculation(config["defs"]["Lead"].pk) is None


def test_non_detect_blanks_stay_not_detected_and_do_not_count_as_zero(config, analyst):
    study = study_for(config, analyst)
    fill(study, analyst, blanks=("ND",) * 7)
    nd = study.observations.filter(kind="blank").first()
    assert nd.result_kind == "non_detect" and nd.value is None and nd.supplied_text == "ND"
    calc = svc.calculate(actor=analyst, study_id=study.pk)
    assert calc.mdl_b is None and calc.mdl_b_method == "not_applicable"
    assert calc.verified_mdl == calc.mdl_s


def test_exclusion_needs_a_documented_reason_and_never_alters_the_observation(config, analyst):
    study = study_for(config, analyst)
    fill(study, analyst, spikes=SPIKES + ("8",))  # eight spikes: one may go
    victim = study.observations.get(kind="spike", supplied_text="8")
    with pytest.raises(ValidationError):
        svc.exclude_observation(actor=analyst, observation_id=victim.pk, reason=" ")
    svc.exclude_observation(actor=analyst, observation_id=victim.pk, reason="Cracked vial")
    victim.refresh_from_db()
    assert victim.excluded and victim.supplied_text == "8" and victim.value == D("8")
    calc = svc.calculate(actor=analyst, study_id=study.pk)
    assert calc.spike_n == 7 and calc.status == "calculated"
    row = next(r for r in calc.inputs if r["id"] == victim.pk)
    assert row["excluded"] and not row["used"] and row["exclusion_reason"] == "Cracked vial"
    spare = study.observations.filter(kind="spike", excluded=False).first()
    svc.exclude_observation(actor=analyst, observation_id=spare.pk, reason="Mislabelled sample")
    again = svc.calculate(actor=analyst, study_id=study.pk)  # now below seven: no partial result
    assert again.status == "not_evaluated" and again.mdl_s is None


def test_observations_and_calculations_are_immutable_after_the_fact(config, analyst, reviewer):
    study = study_for(config, analyst)
    fill(study, analyst)
    calc = svc.calculate(actor=analyst, study_id=study.pk)
    obs = study.observations.first()
    obs.supplied_text = "999"
    with pytest.raises(ValidationError, match="cannot be changed"):
        obs.save()
    calc.verified_mdl = D("0")
    with pytest.raises(ValidationError, match="immutable"):
        calc.save()
    with pytest.raises(ValidationError):
        obs.delete()
    svc.review_calculation(actor=reviewer, calculation_id=calc.pk, decision="adopted")
    reviewed = MDLCalculation.objects.get(pk=calc.pk)
    reviewed.review_note = "edited"
    with pytest.raises(ValidationError, match="immutable"):
        reviewed.save(update_fields=["review_note"])


def test_blanks_import_from_routine_qc_with_provenance_and_no_duplicates(config, analyst):
    batch = start(config, analyst)
    qc = base.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="MB-1")
    lead = config["defs"]["Lead"]
    base.record_qc_measurement(
        actor=analyst,
        qc_sample_id=qc.pk,
        definition_id=lead.pk,
        observed_text="0.0040",
        reporting_limit_applied="0.005",
        analysis_started_at=when(),
    )
    study = study_for(config, analyst)
    today = timezone.localdate()
    count, skipped = svc.import_blanks(
        actor=analyst, study_id=study.pk, start=today - timedelta(days=30), end=today
    )
    obs = study.observations.get()
    assert (count, skipped) == (1, 0) and obs.kind == "blank" and obs.supplied_text == "0.0040"
    assert obs.preparation_label == "MB-1"  # a routine blank is one preparation
    assert obs.source_measurement.qc_sample.label == "MB-1" and obs.batch_label == batch.number
    assert obs.prepared_assumed  # the batch had no preparation time recorded
    assert svc.import_blanks(
        actor=analyst, study_id=study.pk, start=today - timedelta(days=30), end=today
    ) == (0, 0)


def test_verification_can_keep_the_existing_mdl_only_when_the_rule_allows(
    config, analyst, reviewer
):
    first = study_for(config, analyst)
    fill(first, analyst)
    adopted = svc.review_calculation(
        actor=reviewer,
        calculation_id=svc.calculate(actor=analyst, study_id=first.pk).pk,
        decision="adopted",
    )
    second = study_for(config, analyst, kind="verification")
    fill(second, analyst, start_day=3)
    calc = svc.calculate(actor=analyst, study_id=second.pk)
    assert calc.existing_mdl == adopted.adopted_mdl and calc.ratio_to_existing == 1
    assert calc.retention_eligible is True  # same data: ratio 1.0, blanks not above existing MDL
    kept = svc.review_calculation(
        actor=reviewer, calculation_id=calc.pk, decision="retained", note="Within 0.5 to 2.0"
    )
    assert kept.adopted_mdl == adopted.adopted_mdl
    third = study_for(config, analyst, kind="verification")
    fill(third, analyst, spikes=("10", "20", "30", "40", "50", "60", "70"), start_day=2)
    far = svc.calculate(actor=analyst, study_id=third.pk)
    assert far.retention_eligible is False
    with pytest.raises(ValidationError, match="0.5 to 2.0"):
        svc.review_calculation(
            actor=reviewer, calculation_id=far.pk, decision="retained", note="no"
        )
    with pytest.raises(ValidationError):  # a rejection also needs its reason
        svc.review_calculation(actor=reviewer, calculation_id=far.pk, decision="rejected")


def test_roles_have_no_bypass_for_mdl_actions(config, analyst, reviewer, admin_user):
    with pytest.raises(PermissionDenied):
        study_for(config, reviewer)
    study = study_for(config, analyst)
    fill(study, analyst)
    calc = svc.calculate(actor=analyst, study_id=study.pk)
    with pytest.raises(PermissionDenied):
        svc.review_calculation(actor=analyst, calculation_id=calc.pk, decision="adopted")
    with pytest.raises(PermissionDenied):
        svc.calculate(actor=reviewer, study_id=study.pk)
    svc.review_calculation(actor=admin_user, calculation_id=calc.pk, decision="adopted")


# ---- review findings M2, M3, L1, L2 ----------------------------------------------------------


def adopted_initial(config, analyst, reviewer):
    study = study_for(config, analyst)
    fill(study, analyst)
    calc = svc.calculate(actor=analyst, study_id=study.pk)
    return svc.review_calculation(actor=reviewer, calculation_id=calc.pk, decision="adopted")


def test_an_initial_determination_cannot_keep_the_existing_mdl(config, analyst, reviewer):
    adopted_initial(config, analyst, reviewer)
    again = study_for(config, analyst)  # initial re-determination, e.g. after a higher level
    fill(again, analyst, start_day=3)
    calc = svc.calculate(actor=analyst, study_id=again.pk)
    assert calc.status == "calculated" and calc.existing_mdl is None
    assert calc.retention_eligible is None and calc.ratio_to_existing is None
    with pytest.raises(ValidationError, match="belongs to ongoing verification"):
        svc.review_calculation(
            actor=reviewer, calculation_id=calc.pk, decision="retained", note="same"
        )
    adopted = svc.review_calculation(actor=reviewer, calculation_id=calc.pk, decision="adopted")
    assert adopted.adopted_mdl == calc.verified_mdl


def test_a_stale_calculation_cannot_be_adopted_after_new_data_or_a_newer_calculation(
    config, analyst, reviewer, reviewer2
):
    study = study_for(config, analyst)
    fill(study, analyst, spikes=SPIKES + ("8",))
    first = svc.calculate(actor=analyst, study_id=study.pk)
    victim = study.observations.get(kind="spike", supplied_text="8")
    svc.exclude_observation(actor=analyst, observation_id=victim.pk, reason="Cracked vial")
    with pytest.raises(ValidationError, match="added or excluded after this calculation"):
        svc.review_calculation(actor=reviewer, calculation_id=first.pk, decision="adopted")
    second = svc.calculate(actor=analyst, study_id=study.pk)
    with pytest.raises(ValidationError, match="newer calculation exists"):
        svc.review_calculation(actor=reviewer2, calculation_id=first.pk, decision="adopted")
    now = timezone.now()
    svc.add_observation(
        actor=analyst,
        study_id=study.pk,
        kind="blank",
        supplied_text="0.1",
        prepared_at=now - timedelta(hours=3),
        analyzed_at=now - timedelta(hours=2),
        batch_label="S9",
        preparation_label="P9",
    )
    with pytest.raises(ValidationError, match="added or excluded after this calculation"):
        svc.review_calculation(actor=reviewer, calculation_id=second.pk, decision="adopted")
    third = svc.calculate(actor=analyst, study_id=study.pk)
    done = svc.review_calculation(actor=reviewer, calculation_id=third.pk, decision="adopted")
    assert svc.current_calculation(config["defs"]["Lead"].pk).pk == done.pk
    assert not MDLCalculation.objects.get(pk=first.pk).decision  # stale ones stay unreviewed


def test_whoever_excluded_data_cannot_review_the_calculation(config, analyst, reviewer, reviewer2):
    study = study_for(config, analyst)
    fill(study, analyst, spikes=SPIKES + ("8",))
    victim = study.observations.get(kind="spike", supplied_text="8")
    svc.exclude_observation(actor=reviewer, observation_id=victim.pk, reason="Mislabelled sample")
    calc = svc.calculate(actor=analyst, study_id=study.pk)
    with pytest.raises(PermissionDenied, match="excluded data"):
        svc.review_calculation(actor=reviewer, calculation_id=calc.pk, decision="adopted")
    done = svc.review_calculation(actor=reviewer2, calculation_id=calc.pk, decision="adopted")
    assert done.decision == "adopted"


def test_blanks_from_batches_with_rejected_data_are_not_imported(config, analyst, reviewer):
    from analytical.qc import BatchQC

    lead = config["defs"]["Lead"]
    made = []
    for label in ("MB-1", "MB-2"):
        batch = start(config, analyst)
        qc = base.add_qc_sample(
            actor=analyst, batch_id=batch.pk, qc_type="method_blank", label=label
        )
        base.record_qc_measurement(  # above the blank limit, so there is a finding to disposition
            actor=analyst,
            qc_sample_id=qc.pk,
            definition_id=lead.pk,
            observed_text="0.0200",
            reporting_limit_applied="0.005",
            analysis_started_at=when(),
        )
        made.append(batch)
    rejected, kept = made
    key = next(i.key for i in BatchQC.load(rejected).all_issues() if i.key.startswith("m:"))
    base.record_corrective_action(
        actor=analyst,
        batch_id=rejected.pk,
        target_key=key,
        disposition="reject_data",
        note="Batch rejected; samples reanalysed",
    )
    study = study_for(config, analyst)
    today = timezone.localdate()
    span = {"start": today - timedelta(days=5), "end": today}
    assert svc.import_blanks(actor=analyst, study_id=study.pk, **span) == (1, 1)
    assert study.observations.get().batch_label == kept.number
    event = AuditEvent.objects.get(action="mdl.blanks_imported")
    assert event.after["skipped_rejected_batches"] == 1
    # a later acceptance supersedes the rejection (the latest disposition per finding governs)
    base.record_corrective_action(
        actor=reviewer,
        batch_id=rejected.pk,
        target_key=key,
        disposition="accept_qualified",
        note="Accepted after review",
    )
    assert svc.import_blanks(actor=analyst, study_id=study.pk, **span) == (1, 0)


def test_a_rejection_for_another_analyte_does_not_drop_this_tests_blanks(config, analyst):
    lead, copper = config["defs"]["Lead"], config["defs"]["Copper"]
    batch = start(config, analyst)
    qc = base.add_qc_sample(
        actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="MB-SHARED"
    )
    for definition, limit in ((lead, "0.005"), (copper, "0.010")):
        base.record_qc_measurement(
            actor=analyst,
            qc_sample_id=qc.pk,
            definition_id=definition.pk,
            observed_text="0.0900",
            reporting_limit_applied=limit,
            analysis_started_at=when(),
        )
    from analytical.qc import BatchQC

    key = next(i.key for i in BatchQC.load(batch).issues(copper) if i.key.startswith("m:"))
    base.record_corrective_action(
        actor=analyst,
        batch_id=batch.pk,
        target_key=key,
        disposition="reject_data",
        note="Copper blank rejected; lead is unaffected",
    )
    today = timezone.localdate()
    span = {"start": today - timedelta(days=5), "end": today}
    lead_study = study_for(config, analyst)
    assert svc.import_blanks(actor=analyst, study_id=lead_study.pk, **span) == (1, 0)
    copper_study = svc.create_study(
        actor=analyst, definition=copper, kind="initial", spiking_level="5"
    )
    assert svc.import_blanks(actor=analyst, study_id=copper_study.pk, **span) == (0, 1)


def test_an_exclusion_takes_the_study_lock_before_the_observation_lock(config, analyst):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    study = study_for(config, analyst)
    fill(study, analyst)
    victim = study.observations.get(kind="spike", supplied_text="7")
    with CaptureQueriesContext(connection) as queries:
        svc.exclude_observation(actor=analyst, observation_id=victim.pk, reason="Spill")
    locks = [q["sql"] for q in queries if "FOR UPDATE" in q["sql"]]
    assert '"analytical_mdlstudy"' in locks[0] and '"analytical_mdlobservation"' in locks[1]


def test_a_batch_wide_shortfall_rejection_skips_this_tests_blanks(config, analyst):
    """A finding with no test (a QC shortfall) affects every result in the batch, so its
    rejection drops these blanks. Regression: `definition_id__in=[id, None]` lost the NULL case."""
    from analytical.qc import BatchQC

    batch = start(config, analyst)
    qc = base.add_qc_sample(
        actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="MB-SHORT"
    )
    base.record_qc_measurement(  # below the blank limit: no blank finding, only the LCS shortfall
        actor=analyst,
        qc_sample_id=qc.pk,
        definition_id=config["defs"]["Lead"].pk,
        observed_text="ND",
        reporting_limit_applied="0.005",
        analysis_started_at=when(),
    )
    key = next(i.key for i in BatchQC.load(batch).all_issues() if i.key.startswith("s:lcs"))
    action = base.record_corrective_action(
        actor=analyst,
        batch_id=batch.pk,
        target_key=key,
        disposition="reject_data",
        note="No LCS was run; the batch is rejected",
    )
    assert action.definition_id is None  # batch-wide
    study = study_for(config, analyst)
    today = timezone.localdate()
    span = {"start": today - timedelta(days=5), "end": today}
    assert svc.import_blanks(actor=analyst, study_id=study.pk, **span) == (0, 1)
    assert not study.observations.exists()
