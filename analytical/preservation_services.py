"""Batched preservation findings and attributed assessments for authorized staff workflows.

Read callers authorize their page before using summaries/history. All writes authorize here.
Findings concern recorded cooler receipt temperature, never inferred container/chemical facts.
"""

import hashlib
import json
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.roles import require_role
from catalog.models import Method
from core.audit import record_event, snapshot
from receipt.models import Container, RequestedTest, Sample

from .models import PreservationDecision, PreservationRule
from .preservation import screen_temperature

DECISION_ROLES = ("custodian", "project_manager", "reviewer")
TONES = {
    "within": "success",
    "outside": "danger",
    "not_required": "neutral",
    "unconfigured": "warning",
    "manual": "warning",
}


@dataclass
class PreservationSummary:
    status: str
    label: str
    tone: str
    needs_decision: bool
    latest_decision: PreservationDecision | None
    evidence: dict
    fingerprint: str
    blocking_decision: PreservationDecision | None = None


def _number(value):
    if value is None:
        return None
    text = format(value, "f")
    return (text.rstrip("0").rstrip(".") or "0") if "." in text else text


def _observations(sample):
    # Coolant joins the evidence only when recorded: assessments of samples received before
    # the field existed keep their fingerprint, and correcting it later makes them out of date.
    coolant = {"coolant": sample.get_coolant_display()} if sample.coolant else {}
    return {
        **coolant,
        "sample_id": str(sample.pk),
        "lab_id": sample.lab_id,
        "client_sample_id": sample.client_sample_id,
        "matrix": sample.matrix,
        "collection_type": sample.collection_type,
        "collection_started_at": (
            sample.collection_started_at.isoformat() if sample.collection_started_at else None
        ),
        "collected_at": sample.collected_at.isoformat(),
        "received_at": sample.received_at.isoformat(),
        "cooler_temperature": _number(sample.cooler_temperature),
        "cooler_temperature_original": sample.cooler_temperature_original,
        "unit": sample.cooler_temperature_unit,
        "instrument": sample.temperature_instrument,
        "seal_condition": sample.seal_condition,
        "preservation_check": sample.preservation_check,
        "discrepancy": sample.discrepancy,
        "containers": [
            {
                "id": str(c.pk),
                "label": c.label,
                "description": c.description,
                "condition": c.condition,
                "preservation": c.preservation,
            }
            for c in (
                sample._preservation_containers
                if hasattr(sample, "_preservation_containers")
                else sample.containers.order_by("pk")
            )
        ],
    }


def _summaries(sample, tests, rules, decisions):
    observation = _observations(sample)
    out = {}
    for test in tests:
        rule = rules.get(test.definition_id)
        active = rule if rule and rule.is_active else None
        finding = screen_temperature(
            mode=active.temperature_mode if active else None,
            reading=sample.cooler_temperature,
            observed_unit=sample.cooler_temperature_unit,
            unit=active.unit if active else "°C",
            lower=active.lower if active else None,
            upper=active.upper if active else None,
        )
        manual = bool(
            active
            and any(
                getattr(active, name).strip()
                for name in (
                    "container_requirements",
                    "chemical_requirements",
                    "other_requirements",
                )
            )
        )
        evidence = {
            "schema": 1,
            "requested_test_id": test.pk,
            "test_label": str(test.definition),
            "definition_id": test.definition_id,
            "rule": snapshot(rule) if rule else None,
            "observations": observation,
            "temperature": finding,
            "manual_requirements": manual,
        }
        fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
        latest = decisions.get(test.pk)
        current = latest if latest and latest.fingerprint == fingerprint else None
        blocking = latest if latest and latest.decision in {"hold", "reject"} else None
        needs = finding["status"] in {"outside", "manual", "unconfigured"} or manual
        status = (
            "manual"
            if manual and finding["status"] in {"within", "not_required"}
            else finding["status"]
        )
        label = "Manual review required" if status == "manual" else finding["label"]
        out[test.pk] = PreservationSummary(
            status,
            label,
            TONES[status],
            (needs or bool(blocking)) and current is None,
            current,
            evidence,
            fingerprint,
            blocking,
        )
    return out


def _load(sample):
    tests = list(
        RequestedTest.objects.filter(sample=sample)
        .select_related("definition__method", "definition__analyte")
        .order_by("pk")
    )
    rules = {
        r.definition_id: r
        for r in PreservationRule.objects.filter(definition_id__in=[t.definition_id for t in tests])
    }
    history = list(PreservationDecision.objects.filter(sample=sample).select_related("actor"))
    latest = {}
    for decision in history:
        latest.setdefault(decision.evidence.get("requested_test_id"), decision)
    return _summaries(sample, tests, rules, latest), history


def preservation_summaries(sample):
    """One bounded set of queries per sample, not per requested test."""
    summaries, _ = _load(sample)
    return summaries


def preservation_history(sample):
    """Includes withdrawn tests; is_current means its evidence still matches a current test."""
    summaries, history = _load(sample)
    current = {s.fingerprint for s in summaries.values()}
    for decision in history:
        decision.test_label = decision.evidence.get("test_label", "Recorded test")
        decision.is_current = decision.fingerprint in current
    return history


@transaction.atomic
def record_preservation_decision(
    *, actor, requested_test_id, expected_fingerprint, decision, reason, expected_decision_id=None
):
    require_role(actor, *DECISION_ROLES)
    try:
        initial = RequestedTest.objects.select_related("definition").get(pk=requested_test_id)
        method_id = initial.definition.method_id
        Method.objects.select_for_update().get(pk=method_id)
        test = (
            RequestedTest.objects.select_for_update(of=("self",))
            .select_related("definition")
            .get(pk=requested_test_id)
        )
    except RequestedTest.DoesNotExist as exc:
        raise ValidationError("This test is no longer requested. Reload the sample.") from exc
    if test.definition.method_id != method_id:
        raise ValidationError("The requested method changed. Reload before assessing.")
    sample = Sample.objects.select_for_update().get(pk=test.sample_id)
    summary = preservation_summaries(sample)[test.pk]
    if summary.fingerprint != expected_fingerprint:
        raise ValidationError(
            "The preservation rule or receipt evidence changed. Reload and assess again."
        )
    current_id = summary.latest_decision.pk if summary.latest_decision else None
    if current_id != expected_decision_id:
        raise ValidationError(
            "Another preservation assessment was recorded. Reload before deciding."
        )
    if decision not in PreservationDecision.Decision.values:
        raise ValidationError({"decision": "Choose a laboratory disposition."})
    reason = (reason or "").strip()
    if not reason or len(reason) > 4000:
        raise ValidationError(
            {"reason": "Give assessment evidence and a reason of at most 4000 characters."}
        )
    record = PreservationDecision(
        sample=sample,
        definition_id=test.definition_id,
        evidence=summary.evidence,
        fingerprint=summary.fingerprint,
        decision=decision,
        reason=reason,
        actor=actor,
    )
    record.full_clean()
    record.save()
    record_event(
        actor=actor,
        action="preservation.assessed",
        obj=record,
        before={
            "previous_decision_id": summary.latest_decision.pk if summary.latest_decision else None
        },
        after={
            "decision": decision,
            "fingerprint": summary.fingerprint,
            "requested_test_id": test.pk,
            "sample_id": str(sample.pk),
        },
        reason=reason,
    )
    return record


def summaries_for_tests(tests):
    """Three queries for an already-loaded test/sample set, including external-only tests."""
    tests = list(tests)
    samples = {t.sample_id: t.sample for t in tests}
    rules = {
        r.definition_id: r
        for r in PreservationRule.objects.filter(definition_id__in={t.definition_id for t in tests})
    }
    decisions = {}
    for d in PreservationDecision.objects.filter(sample_id__in=samples).select_related("actor"):
        decisions.setdefault(d.sample_id, {}).setdefault(d.evidence.get("requested_test_id"), d)
    for sample in samples.values():
        sample._preservation_containers = []
    for c in Container.objects.filter(sample_id__in=samples).order_by("pk"):
        samples[c.sample_id]._preservation_containers.append(c)
    grouped = {}
    for test in tests:
        grouped.setdefault(test.sample_id, []).append(test)
    out = {}
    for sample_id, group in grouped.items():
        out.update(_summaries(samples[sample_id], group, rules, decisions.get(sample_id, {})))
    return out


def preservation_blockers(summary):
    decision = summary.blocking_decision or summary.latest_decision
    if decision and decision.decision in {"hold", "reject"}:
        earlier = (
            " Recorded on earlier evidence; reassess to lift it."
            if (decision.fingerprint != summary.fingerprint)
            else ""
        )
        return [
            f"Preservation disposition is {decision.get_decision_display().lower()}: "
            f"{decision.reason}.{earlier}"
        ]
    if summary.needs_decision:
        return [f"Preservation: {summary.label}. Record a current reasoned lab disposition."]
    return []


def preservation_snapshot(summary):
    decision = summary.latest_decision
    return {
        "status": summary.status,
        "label": summary.label,
        "fingerprint": summary.fingerprint,
        "evidence": summary.evidence,
        "decision": None
        if decision is None
        else {
            "id": decision.pk,
            "decision": decision.decision,
            "label": decision.get_decision_display(),
            "reason": decision.reason,
            "actor": decision.actor.get_full_name() or decision.actor.get_username(),
            "recorded_at": decision.recorded_at.isoformat(),
        },
    }
