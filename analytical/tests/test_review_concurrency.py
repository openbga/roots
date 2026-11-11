"""Submit, approve and return on one batch under real concurrency (two PostgreSQL connections).

The first decision is held inside its transaction after its writes; the second must wait for the
batch and result locks, then see the committed state and be refused.
"""

import pytest
from django.core.exceptions import ValidationError

from analytical import services as svc
from analytical.models import ResultRevision
from core.models import AuditEvent
from laboratory.tests.test_concurrency import Hold, race

from .test_workflow import entered, prepared, run_qc, start

pytestmark = pytest.mark.django_db(transaction=True)


def test_two_submits_of_one_result_record_one(monkeypatch, config, analyst, analyst2):
    batch = start(config, analyst)
    run_qc(config, analyst, batch)
    rev = entered(config, analyst, batch)
    first, second = race(
        Hold(monkeypatch, svc),
        lambda: svc.submit_result(actor=analyst, revision_id=rev.pk),
        lambda: svc.submit_result(actor=analyst2, revision_id=rev.pk),
    )
    assert isinstance(first, ResultRevision)
    assert isinstance(second, ValidationError), second
    rev.refresh_from_db()
    assert (rev.status, rev.submitted_by_id) == ("submitted", analyst.pk)
    assert AuditEvent.objects.filter(action="result.submitted").count() == 1


DECIDE = {
    "approve": lambda actor, rev: svc.approve_result(actor=actor, revision_id=rev.pk),
    "return": lambda actor, rev: svc.return_result(
        actor=actor, revision_id=rev.pk, reason="Check the dilution"
    ),
}
STATUS = {"approve": "approved", "return": "returned"}


@pytest.mark.parametrize(
    "first_action, second_action",
    [("approve", "return"), ("return", "approve"), ("return", "return")],
)
def test_one_review_decision_wins(
    monkeypatch, config, analyst, reviewer, reviewer2, first_action, second_action
):
    batch, rev = prepared(config, analyst)
    first, second = race(
        Hold(monkeypatch, svc),
        lambda: DECIDE[first_action](reviewer, rev),
        lambda: DECIDE[second_action](reviewer2, rev),
    )
    assert isinstance(first, ResultRevision)
    assert isinstance(second, ValidationError) and "not awaiting review" in str(second), second
    rev.refresh_from_db()
    rev.result.refresh_from_db()
    assert (rev.status, rev.reviewed_by_id) == (STATUS[first_action], reviewer.pk)
    assert rev.result.status == STATUS[first_action]
    decisions = AuditEvent.objects.filter(action__in=["result.approved", "result.returned"])
    assert decisions.count() == 1
