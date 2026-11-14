"""Holding-time limits per analyte: which rule applies to a test, at entry and in the queues."""

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from analytical import holding, selectors
from analytical import services as svc
from analytical.models import HoldingTimeRule, PreparationRequirement
from catalog import services as catalog_services
from catalog.models import Analyte, Method, TestDefinition
from core.models import AuditEvent
from receipt.models import RequestedTest

from .test_workflow import entry, start

pytestmark = pytest.mark.django_db

# The config fixture: samples collected 3 days ago, one method-wide rule of 14 days.


def rule(config, analyte=None, *, days=1, start_event="collection", end_event="analysis", **kw):
    return HoldingTimeRule.objects.create(
        method=config["method"],
        matrix="water",
        analyte=config["defs"][analyte].analyte if analyte else None,
        name=f"{analyte or 'Method'} {start_event} to {end_event}",
        start_event=start_event,
        end_event=end_event,
        duration=timedelta(days=days),
        **kw,
    )


def enter(config, analyst, batch, analyte, **kw):
    result = batch.results.get(requested_test__definition=config["defs"][analyte])
    return svc.enter_result(actor=analyst, result_id=result.pk, **entry(**kw))


def test_an_analyte_rule_replaces_the_method_rule_for_that_analyte_only(config, analyst):
    method_rule = HoldingTimeRule.objects.get()
    copper = rule(config, "Copper", days=1)
    batch = start(config, analyst)
    lead_rev = enter(config, analyst, batch, "Lead")
    copper_rev = enter(config, analyst, batch, "Copper")
    assert lead_rev.holding_status == "met"
    assert [c["rule_id"] for c in lead_rev.holding_evaluation] == [method_rule.pk]
    # One clock, one check: the analyte's limit, not the method's as well.
    assert copper_rev.holding_status == "exceeded"
    assert [c["rule_id"] for c in copper_rev.holding_evaluation] == [copper.pk]
    with pytest.raises(ValidationError, match="no holding-time qualifier"):
        svc.submit_result(actor=analyst, revision_id=copper_rev.pk)
    svc.submit_result(actor=analyst, revision_id=lead_rev.pk)


def test_a_longer_analyte_rule_lifts_the_method_limit_for_that_analyte(config, analyst):
    HoldingTimeRule.objects.update(duration=timedelta(days=1))
    rule(config, "Copper", days=28)
    batch = start(config, analyst)
    assert enter(config, analyst, batch, "Lead").holding_status == "exceeded"
    assert enter(config, analyst, batch, "Copper").holding_status == "met"


def test_other_clocks_and_inactive_analyte_rules_leave_the_method_rule_in_force(config, analyst):
    method_rule = HoldingTimeRule.objects.get()
    copper = config["defs"]["Copper"].analyte
    to_prep = rule(config, "Copper", days=2, end_event="preparation")
    applied = {r.pk for r in holding.active_rules(config["method"], "water", copper)}
    assert applied == {method_rule.pk, to_prep.pk}  # a different clock replaces nothing
    off = rule(config, "Copper", days=1, is_active=False)
    applied = {r.pk for r in holding.active_rules(config["method"], "water", copper.pk)}
    assert applied == {method_rule.pk, to_prep.pk}
    off.is_active = True
    off.save()
    applied = {r.pk for r in holding.active_rules(config["method"], "water", copper)}
    assert applied == {off.pk, to_prep.pk}
    method_rule.is_active = False  # an analyte's rule stands without a method-wide one
    method_rule.save()
    lead = config["defs"]["Lead"].analyte
    assert holding.active_rules(config["method"], "water", lead) == []
    batch = start(config, analyst)
    assert enter(config, analyst, batch, "Lead").holding_status == "not_evaluated"
    copper_rev = enter(config, analyst, batch, "Copper")
    assert {c["rule_id"]: c["status"] for c in copper_rev.holding_evaluation} == {
        off.pk: "exceeded",
        to_prep.pk: "not_evaluable",  # the batch records no preparation time
    }


def test_queue_deadline_and_filters_use_the_same_rule_as_entry(config):
    """The SQL annotation and holding.active_rules are one definition (holding.for_analyte)."""
    rule(config, "Copper", days=1, warning_window=timedelta(hours=6))
    rule(config, "Lead", days=30, end_event="preparation")  # collection-start, another clock
    rule(config, "Lead", days=2, is_active=False)
    tests = selectors.with_holding_deadline(
        RequestedTest.objects.select_related("definition", "sample")
    )
    assert tests.count() == 4
    for rt in tests:
        applied = [
            r
            for r in holding.active_rules(
                rt.definition.method, rt.definition.matrix, rt.definition.analyte_id
            )
            if r.start_event == "collection"
        ]
        shortest = min(applied, key=lambda r: r.duration)
        assert rt.hold_limit == shortest.duration, rt.definition
        assert rt.hold_warning == shortest.warning_window
        assert rt.hold_deadline == rt.sample.collected_at + shortest.duration
    by_analyte = {rt.definition.analyte.name.split()[0]: rt.hold_limit for rt in tests}
    assert by_analyte == {"Lead": timedelta(days=14), "Copper": timedelta(days=1)}
    expired = selectors.worklist_queryset(holding="expired")
    assert {rt.definition.analyte.name.split()[0] for rt in expired} == {"Copper"}
    assert expired.count() == 2  # both samples' copper tests, and no lead test


def test_sample_page_shows_each_test_against_its_own_rule(config, client, analyst):
    rule(config, "Copper", days=1)
    client.force_login(analyst)
    sample = config["samples"][0]
    html = client.get(reverse("receipt:sample-detail", args=[sample.pk])).content.decode()
    for days in (1, 14):
        deadline = timezone.localtime(sample.collected_at + timedelta(days=days))
        assert f'datetime="{deadline.isoformat()}"' in html
    assert html.count('data-vr-clock="expired"') == 1  # copper only


def test_scope_is_unique_and_the_analyte_must_be_tested_by_the_method(config, admin_user):
    method_rule = HoldingTimeRule.objects.get()
    copper = config["defs"]["Copper"].analyte

    def new(analyte, matrix="water"):
        return HoldingTimeRule(
            method=config["method"],
            matrix=matrix,
            analyte=analyte,
            name="Second",
            start_event="collection",
            end_event="analysis",
            duration=timedelta(days=2),
        )

    saved = svc.save_config(actor=admin_user, instance=new(copper))
    for duplicate in (new(None), new(copper)):
        with pytest.raises(ValidationError, match="already exists for this method, matrix"):
            svc.save_config(actor=admin_user, instance=duplicate)
        # The database refuses it too: two rules without an analyte are not distinct.
        with pytest.raises(IntegrityError), transaction.atomic():
            duplicate.save()
    stranger = Analyte.objects.create(name="Zinc (synthetic)")
    with pytest.raises(ValidationError) as refused:
        svc.save_config(actor=admin_user, instance=new(stranger))
    assert "no test for this analyte" in refused.value.message_dict["analyte"][0]
    with pytest.raises(ValidationError) as refused:  # tested in water, not in soil
        svc.save_config(actor=admin_user, instance=new(copper, matrix="soil"))
    assert "analyte" in refused.value.message_dict
    assert HoldingTimeRule.objects.count() == 2
    assert {method_rule.applies_to, saved.applies_to} == {"All analytes", "Copper (synthetic)"}
    event = AuditEvent.objects.get(action="config.holdingtimerule.created")
    assert event.after["analyte"] == copper.pk and event.actor == admin_user
    # The analyte's catalog page counts the rule among what depends on it.
    assert ("Holding-time rules", 1) in catalog_services.usage_facts(copper)


def test_an_analyte_rule_on_a_preparation_method_needs_a_test_it_prepares(config, admin_user):
    digest = Method.objects.create(code="DEMO-DIGEST", version="r1", title="Synthetic digestion")
    copper = config["defs"]["Copper"]

    def prep_rule():
        return HoldingTimeRule(
            method=digest,
            matrix="water",
            analyte=copper.analyte,
            name="Copper collection to digestion",
            start_event="collection",
            end_event="preparation",
            duration=timedelta(days=2),
        )

    with pytest.raises(ValidationError):
        svc.save_config(actor=admin_user, instance=prep_rule())
    PreparationRequirement.objects.create(definition=copper, method=digest, basis="SYNTHETIC")
    saved = svc.save_config(actor=admin_user, instance=prep_rule())
    # At entry the preparation method's rules are resolved for the result's analyte as well.
    status, checks = holding.evaluate_for_entry(
        method=config["method"],
        matrix="water",
        analyte=copper.analyte_id,
        collected_at=timezone.now() - timedelta(days=3),
        prepared_at=timezone.now() - timedelta(hours=2),
        analysis_at=timezone.now() - timedelta(hours=1),
        extra_method_ids=[digest.pk],
    )
    assert status == "exceeded"
    assert {c["rule_id"]: c["status"] for c in checks} == {
        HoldingTimeRule.objects.get(method=config["method"]).pk: "met",
        saved.pk: "exceeded",
    }
    _, lead_checks = holding.evaluate_for_entry(
        method=config["method"],
        matrix="water",
        analyte=config["defs"]["Lead"].analyte_id,
        collected_at=timezone.now() - timedelta(days=3),
        prepared_at=timezone.now() - timedelta(hours=2),
        analysis_at=timezone.now() - timedelta(hours=1),
        extra_method_ids=[digest.pk],
    )
    assert [c["rule_id"] for c in lead_checks] == [
        HoldingTimeRule.objects.get(method=config["method"]).pk
    ]


def test_preparation_notice_lists_only_rules_that_apply_to_the_batch(config, analyst):
    HoldingTimeRule.objects.all().delete()
    method_prep = rule(config, days=7, start_event="preparation")
    copper_prep = rule(config, "Copper", days=2, start_event="preparation")
    empty = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    assert selectors.preparation_rules_missing(empty) == [method_prep]
    lead_test = RequestedTest.objects.get(
        sample=config["samples"][0], definition=config["defs"]["Lead"]
    )
    copper_test = RequestedTest.objects.get(
        sample=config["samples"][0], definition=config["defs"]["Copper"]
    )
    svc.add_tests_to_batch(actor=analyst, batch_id=empty.pk, requested_test_ids=[copper_test.pk])
    assert selectors.preparation_rules_missing(empty) == [copper_prep]  # replaced for copper
    svc.add_tests_to_batch(actor=analyst, batch_id=empty.pk, requested_test_ids=[lead_test.pk])
    assert set(selectors.preparation_rules_missing(empty)) == {method_prep, copper_prep}
    empty.prepared_at = timezone.now()
    assert selectors.preparation_rules_missing(empty) == []


def test_rule_form_sets_the_analyte_and_the_table_shows_what_a_rule_applies_to(
    config, client, admin_user, analyst
):
    url = reverse("analytical:config-new", args=["holding-rule"])
    copper = config["defs"]["Copper"].analyte
    data = {
        "method": config["method"].pk,
        "matrix": "water",
        "analyte": copper.pk,
        "name": "Copper collection to analysis",
        "start_event": "collection",
        "end_event": "analysis",
        "duration_hours": "48",
        "warning_hours": "",
        "basis": "SYNTHETIC TEST VALUE",
        "is_active": "on",
        "reason": "",
    }
    client.force_login(analyst)
    assert client.post(url, data).status_code == 403
    with pytest.raises(PermissionDenied):
        svc.save_config(actor=analyst, instance=HoldingTimeRule.objects.get(), reason="x")
    client.force_login(admin_user)
    wrong = Analyte.objects.create(name="Zinc (synthetic)")
    refused = client.post(url, {**data, "analyte": wrong.pk})
    assert refused.status_code == 200
    assert "no test for this analyte" in refused.context["form"].errors["analyte"][0]
    assert refused.context["form"]["name"].value() == data["name"]  # input kept
    assert client.post(url, data).status_code == 302
    created = HoldingTimeRule.objects.get(analyte=copper)
    assert created.duration == timedelta(hours=48)
    page = client.get(reverse("analytical:qc-config")).content.decode()
    assert 'data-vr-col="applies_to"' in page
    assert "Copper (synthetic)</td>" in page and "All analytes</span></td>" in page


def test_seeds_and_a_new_test_keep_working_beside_analyte_rules(config, admin_user):
    """A method-wide lookup must not trip over an analyte's rule of the same clock."""
    from analytical.seed import seed_analytical_config
    from core.models import Lab

    rule(config, "Copper", days=1)
    Lab.objects.update_or_create(pk=1, defaults={"name": "Example Environmental Lab"})
    assert seed_analytical_config() >= 1
    assert HoldingTimeRule.objects.filter(method=config["method"]).count() == 2
    zinc = Analyte.objects.create(name="Zinc (synthetic)")
    definition = TestDefinition.objects.create(
        method=config["method"], analyte=zinc, matrix="water", unit="mg/L"
    )
    rt = selectors.with_holding_deadline(
        RequestedTest.objects.filter(
            pk=RequestedTest.objects.create(sample=config["samples"][0], definition=definition).pk
        )
    ).get()
    assert rt.hold_limit == timedelta(days=14)  # a new analyte falls under the method rule


def test_reversing_the_migration_refuses_while_a_rule_names_an_analyte(config):
    """Without the column an analyte's rule would silently become a method-wide rule."""
    from importlib import import_module
    from types import SimpleNamespace

    from django.apps import apps
    from django.db import connection

    migration = import_module("analytical.migrations.0009_holding_rule_analyte")
    editor = SimpleNamespace(connection=connection)
    migration.refuse_reverse_with_analyte_rules(apps, editor)  # only method-wide rules: fine
    rule(config, "Copper", days=1)
    with pytest.raises(RuntimeError, match="1 holding-time rule.s. name an analyte"):
        migration.refuse_reverse_with_analyte_rules(apps, editor)
