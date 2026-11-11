"""Client, project, chain-of-custody and sample master data added for intake.

Covers the rules a plausible implementation gets wrong: who may be a project manager, where a
purchase order comes from, reasoned and audited detail edits, a sample source that must belong
to its matrix, and list fields that keep accepting values outside the list.
"""

import pytest
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from core.models import AuditEvent
from core.widgets import OTHER, ListOrOtherField
from receipt.corrections import correct_sample, correct_sample_matrix
from receipt.forms import ContainerForm, OrderSampleForm, ProjectEditForm
from receipt.models import Client, Order, Sample
from receipt.services import (
    create_client,
    create_order,
    create_project,
    update_client,
    update_order_details,
    update_project,
)

pytestmark = pytest.mark.django_db


def test_client_master_data_is_audited_and_account_code_is_unique(intake):
    manager = intake["user"]("md_pm", "project_manager")
    client = create_client(
        actor=manager,
        name="Synthetic Water Authority",
        account_number="SWA",
        address_line1="1 Synthetic Way",
        city="Exampleville",
        state="NY",
        postal_code="00000",
        payment_terms="net_30",
        po_required=True,
    )
    assert client.address_lines == ["1 Synthetic Way", "Exampleville, NY 00000"]
    assert AuditEvent.objects.get(action="client.created").after["account_number"] == "SWA"
    with pytest.raises(ValidationError, match="already uses this account code"):
        create_client(actor=manager, name="Second Synthetic Client", account_number="SWA")
    # Clients without a code never collide with each other.
    create_client(actor=manager, name="Uncoded Synthetic Client")
    assert Client.objects.filter(account_number="").count() >= 2
    updated = update_client(
        actor=manager,
        client_id=client.pk,
        expected_revision=1,
        reason="Accounts payable moved",
        billing_email="payables@example.invalid",
        payment_terms="net_45",
    )
    event = AuditEvent.objects.get(action="client.updated")
    assert updated.revision == 2 and updated.po_required is True
    assert (event.before["payment_terms"], event.after["payment_terms"]) == ("net_30", "net_45")


def test_project_manager_must_be_laboratory_staff(intake):
    manager = intake["user"]("md_pm2", "project_manager")
    portal = intake["user"]("md_portal", client=intake["client"])
    with pytest.raises(ValidationError, match="laboratory staff"):
        create_project(
            actor=manager, client=intake["client"], name="Portal managed", project_manager=portal
        )
    project = create_project(
        actor=manager,
        client=intake["client"],
        name="Synthetic Outfall",
        number="SYN-7",
        regulatory_program="npdes",
        site_state="NY",
        project_manager=manager,
    )
    assert AuditEvent.objects.get(action="project.created").after["project_manager"] == manager.pk
    with pytest.raises(ValidationError, match="laboratory staff"):
        update_project(
            actor=manager,
            project_id=project.pk,
            expected_revision=1,
            reason="Reassign",
            project_manager=portal,
        )
    # A manager whose account is later deactivated stays on the project and in its edit form.
    former = intake["user"]("md_former_pm", "project_manager")
    update_project(
        actor=manager,
        project_id=project.pk,
        expected_revision=1,
        reason="Handover",
        project_manager=former,
    )
    former.is_active = False
    former.save()
    project.refresh_from_db()
    assert former in ProjectEditForm(instance=project).fields["project_manager"].queryset
    kept = update_project(
        actor=manager,
        project_id=project.pk,
        expected_revision=2,
        reason="Site renamed",
        site_address="Synthetic plant B",
        project_manager=former,
    )
    assert kept.project_manager == former


def test_order_takes_project_po_and_detail_edits_are_reasoned_and_audited(intake):
    custodian, project = intake["actor"], intake["project"]
    project.po_number = "PO-STANDING"
    project.save()
    inherited = create_order(actor=custodian, project=project, coc_reference="COC-MD-1")
    own = create_order(
        actor=custodian,
        project=project,
        coc_reference="COC-MD-2",
        po_number="PO-OWN",
        priority="rush",
        delivery_method="carrier",
        carrier_tracking="SYN-TRACK-1",
        cooler_count=2,
    )
    assert inherited.po_number == "PO-STANDING" and inherited.priority == "standard"
    assert own.po_number == "PO-OWN" and own.is_rush
    with pytest.raises(ValidationError, match="Unknown chain of custody details"):
        create_order(actor=custodian, project=project, coc_reference="COC-MD-3", due_on="x")

    base = dict(actor=custodian, order_id=own.pk, expected_revision=own.revision)
    with pytest.raises(ValidationError, match="reason"):
        update_order_details(**base, reason=" ", cooler_count=3)
    with pytest.raises(ValidationError, match="Change at least one"):
        update_order_details(**base, reason="Nothing", cooler_count=2)
    with pytest.raises(PermissionDenied):
        update_order_details(
            **base | {"actor": intake["user"]("md_analyst", "analyst")},
            reason="Not my role",
            cooler_count=3,
        )
    with pytest.raises(ValidationError, match="Unknown chain of custody details"):
        update_order_details(**base, reason="Wrong path", coc_reference="OTHER")
    with pytest.raises(ValidationError, match="less than or equal to 200"):
        update_order_details(**base, reason="Typo", cooler_count=2000)
    saved = update_order_details(
        **base, reason="Second cooler arrived late", cooler_count=3, priority="standard"
    )
    event = AuditEvent.objects.get(action="order.details_updated")
    assert saved.revision == own.revision + 1 and not saved.is_rush
    assert (event.before["cooler_count"], event.after["cooler_count"]) == (2, 3)
    assert event.before["po_number"] == event.after["po_number"] == "PO-OWN"
    assert event.reason == "Second cooler arrived late"
    with pytest.raises(ValidationError, match="Order changed"):
        update_order_details(**base, reason="Stale form", cooler_count=4)


def test_order_edit_page_requires_intake_role_and_refuses_cancelled_orders(client, intake):
    order = create_order(actor=intake["actor"], project=intake["project"], coc_reference="COC-MD-4")
    url = reverse("receipt:order-edit", args=[order.pk])
    client.force_login(intake["user"]("md_reviewer", "reviewer"))
    assert client.get(url).status_code == 403
    client.force_login(intake["actor"])
    response = client.post(
        url,
        {
            "expected_revision": order.revision,
            "priority": "rush",
            "po_number": "PO-HTTP",
            "delivery_method": "client",
            "carrier_tracking": "",
            "cooler_count": "1",
            "instructions": "Synthetic: call before disposal.",
            "reason": "Details from the paperwork",
        },
    )
    assert response.status_code == 302
    order.refresh_from_db()
    assert (order.po_number, order.priority, order.cooler_count) == ("PO-HTTP", "rush", 1)
    detail = client.get(reverse("receipt:order-detail", args=[order.pk]))
    assert b"PO-HTTP" in detail.content and b"Synthetic: call before disposal." in detail.content
    Order.objects.filter(pk=order.pk).update(
        cancelled_at=order.created_at, cancelled_by=intake["actor"], cancel_reason="Never arrived"
    )
    assert client.get(url).status_code == 302  # back to the order with an explanation


def test_sample_source_must_belong_to_matrix_at_receipt_and_in_corrections(intake):
    with pytest.raises(ValidationError, match="belongs to the sample matrix"):
        intake["sample"](source_type="soil")
    sample = intake["sample"](
        source_type="groundwater", sample_type="field_duplicate", coolant="wet_ice"
    )
    assert sample.get_source_type_display() == "Groundwater"
    assert AuditEvent.objects.get(action="sample.received").after["sample_type"] == (
        "field_duplicate"
    )
    base = dict(actor=intake["actor"], sample_id=sample.pk, section="identity")
    with pytest.raises(ValidationError, match="belongs to the sample matrix"):
        correct_sample(
            **base, expected_revision=sample.revision, reason="Wrong", source_type="sludge"
        )
    corrected = correct_sample(
        **base,
        expected_revision=sample.revision,
        reason="The chain of custody says trip blank",
        sample_type="trip_blank",
        source_type="",
        remarks="Synthetic: laboratory-supplied blank water.",
    )
    event = AuditEvent.objects.get(action="sample.identity_corrected")
    assert (event.before["sample_type"], event.after["sample_type"]) == (
        "field_duplicate",
        "trip_blank",
    )
    assert corrected.source_type == "" and corrected.remarks.startswith("Synthetic")


def test_matrix_correction_clears_a_source_of_the_old_matrix(intake):
    from catalog.models import TestDefinition

    sample = intake["sample"](source_type="groundwater")
    soil = TestDefinition.objects.create(
        method=intake["method"],
        analyte=intake["definition"].analyte,
        matrix="soil",
        unit="mg/kg",
    )
    corrected = correct_sample_matrix(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        matrix="soil",
        tests=[soil],
        reason="Jar of soil was logged as water",
    )
    event = AuditEvent.objects.get(action="sample.matrix_corrected")
    assert corrected.source_type == ""
    assert (event.before["source_type"], event.after["source_type"]) == ("groundwater", "")


def test_receipt_form_requires_coolant_and_defaults_sample_type():
    form = OrderSampleForm({"matrix": "water", "source_type": "sediment"})
    assert not form.is_valid()
    assert "coolant" in form.errors
    assert "does not belong to Water" in form.errors["source_type"][0]
    assert "sample_type" not in form.errors
    assert form.cleaned_data["sample_type"] == "sample"


class _ListForm(forms.Form):
    seal = ListOrOtherField(("Intact", "Broken or damaged"), max_length=20)
    role = ListOrOtherField(("Sampler",), max_length=20, required=False)


def test_list_or_other_field_accepts_listed_other_and_legacy_values():
    listed = _ListForm({"seal": "Intact"})
    assert listed.is_valid() and listed.cleaned_data == {"seal": "Intact", "role": ""}
    other = _ListForm({"seal": OTHER, "seal_other": "  Tape cut by courier  "})
    assert other.is_valid() and other.cleaned_data["seal"] == "Tape cut by courier"
    # A plain value outside the list (older clients, values recorded before the list) is kept.
    legacy = _ListForm({"seal": "Checked"})
    assert legacy.is_valid() and legacy.cleaned_data["seal"] == "Checked"
    blank_other = _ListForm({"seal": OTHER, "seal_other": " "})
    assert not blank_other.is_valid() and "Describe the value" in blank_other.errors["seal"][0]
    too_long = _ListForm({"seal": OTHER, "seal_other": "x" * 21})
    assert not too_long.is_valid()
    # An unlisted stored value is shown as "Other" with its text, never dropped.
    html = str(_ListForm(initial={"seal": "Checked"})["seal"])
    assert f'value="{OTHER}" selected' in html and 'value="Checked"' in html
    assert 'name="seal_other"' in html and 'maxlength="20"' in html
    assert "maxlength" not in html.split("</select>")[0]


def test_blank_container_rows_stay_ignored_with_list_controls():
    from receipt.forms import ContainerFormSet

    data = {
        "containers-TOTAL_FORMS": "2",
        "containers-INITIAL_FORMS": "0",
        "containers-0-description": "1 L amber glass",
        "containers-0-preservation": OTHER,
        "containers-0-preservation_other": "Synthetic buffer",
        "containers-0-condition": "Intact",
        "containers-1-description": "",
        "containers-1-preservation": "",
        "containers-1-condition": "",
    }
    formset = ContainerFormSet(data, prefix="containers")
    assert formset.is_valid(), formset.errors
    rows = [row for row in formset.cleaned_data if row]
    assert [(r["description"], r["preservation"], r["condition"]) for r in rows] == [
        ("1 L amber glass", "Synthetic buffer", "Intact")
    ]
    assert not ContainerForm({"description": "1 L amber glass"}).is_valid()  # condition required


def test_new_sample_columns_default_for_rows_written_by_the_previous_release(intake):
    """Old and new instances overlap during a deploy: an insert that names none of the new
    columns must still succeed with the documented defaults."""
    from django.db import connection

    sample = intake["sample"]()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO receipt_order (id, project_id, coc_reference, revision, created_at, "
            "created_by_id) VALUES (gen_random_uuid(), %s, 'COC-OLD-RELEASE', 1, now(), %s) "
            "RETURNING id",
            [intake["project"].pk, intake["actor"].pk],
        )
        order = Order.objects.get(pk=cursor.fetchone()[0])
        cursor.execute(
            "INSERT INTO receipt_client (name, contact_name, email, report_preferences, "
            "is_active, revision) VALUES ('Old Release Client', '', '', '', true, 1) RETURNING id"
        )
        old_client = Client.objects.get(pk=cursor.fetchone()[0])
    assert (order.priority, order.po_number, order.cooler_count) == ("standard", "", None)
    assert (old_client.account_number, old_client.po_required) == ("", False)
    assert Sample.objects.get(pk=sample.pk).sample_type == "sample"
