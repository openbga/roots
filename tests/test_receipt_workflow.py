from concurrent.futures import ThreadPoolExecutor

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connections
from django.urls import reverse

from core.models import AuditEvent
from receipt.forms import ContainerFormSet
from receipt.models import Client, Order, Project, Sample
from receipt.services import (
    allocate_accession,
    change_disposition,
    create_order,
    receive_order_sample,
    receive_sample,
    update_client,
    update_project,
)


def sample_inputs(data):
    return {
        key: value
        for key, value in data.items()
        if key not in {"project", "coc_reference", "container_description", "container_condition"}
    }


def containers():
    return [
        {"description": "Bottle A", "condition": "Intact", "preservation": "As supplied"},
        {"description": "Bottle B", "condition": "Intact", "preservation": "As supplied"},
    ]


@pytest.mark.django_db
def test_one_coc_continues_with_multiple_samples_and_containers(staff, receipt_data):
    actor = staff("custodian", "custodian")
    order = create_order(actor=actor, project=receipt_data["project"], coc_reference="COC-MULTI")
    first = receive_order_sample(
        actor=actor,
        order_id=order.pk,
        expected_revision=1,
        containers=containers(),
        **sample_inputs(receipt_data),
    )
    second = receive_order_sample(
        actor=actor,
        order_id=order.pk,
        expected_revision=2,
        containers=containers()[:1],
        **{
            **sample_inputs(receipt_data),
            "client_sample_id": "SECOND",
            "collection_location": "Synthetic outfall south bank",
        },
    )
    assert Order.objects.count() == 1
    assert order.samples.count() == 2
    assert first.containers.count() == 2 and second.containers.count() == 1
    assert first.lab_id.startswith("VR-") and first.lab_id[3:].isdigit()
    assert int(second.lab_id[3:]) > int(first.lab_id[3:])
    assert first.id != second.id
    assert first.collection_location != second.collection_location
    event = AuditEvent.objects.get(object_id=str(first.pk), action="sample.received")
    assert len(event.after["containers"]) == 2
    assert event.after["collection_location"] == receipt_data["collection_location"]
    assert first.custody_events.count() == second.custody_events.count() == 1
    with pytest.raises(ValidationError, match="Order changed"):
        receive_order_sample(
            actor=actor,
            order_id=order.pk,
            expected_revision=2,
            containers=containers(),
            **sample_inputs(receipt_data),
        )
    assert order.samples.count() == 2


@pytest.mark.django_db
def test_invalid_second_container_rolls_back_entire_sample(staff, receipt_data):
    actor = staff("custodian", "custodian")
    order = create_order(actor=actor, project=receipt_data["project"], coc_reference="COC-ROLLBACK")
    with pytest.raises(ValidationError):
        receive_order_sample(
            actor=actor,
            order_id=order.pk,
            expected_revision=1,
            containers=[containers()[0], {"description": "", "condition": "Intact"}],
            **sample_inputs(receipt_data),
        )
    order.refresh_from_db()
    assert order.revision == 1 and not order.samples.exists()
    assert not AuditEvent.objects.filter(action="sample.received").exists()


@pytest.mark.django_db
def test_reasoned_disposition_change_preserves_history_and_rejects_stale_write(staff, receipt_data):
    actor = staff("custodian", "custodian")
    sample = receive_sample(
        actor=actor,
        **{
            **receipt_data,
            "disposition": "hold",
            "discrepancy": "Seal broken",
            "disposition_note": "Await client instruction",
        },
    )
    with pytest.raises(PermissionDenied):
        change_disposition(
            actor=staff("analyst", "analyst"),
            sample_id=sample.pk,
            expected_revision=1,
            disposition="proceed",
            reason="Client response",
        )
    with pytest.raises(ValidationError):
        change_disposition(
            actor=actor, sample_id=sample.pk, expected_revision=1, disposition="qualify", reason=" "
        )
    change_disposition(
        actor=actor,
        sample_id=sample.pk,
        expected_revision=1,
        disposition="qualify",
        reason="Client requests analysis with seal qualification",
    )
    sample.refresh_from_db()
    assert sample.disposition == "qualify" and sample.discrepancy == "Seal broken"
    event = AuditEvent.objects.get(action="sample.disposition_changed")
    assert event.actor == actor and event.before["disposition"] == "hold"
    assert event.before["disposition_note"] == "Await client instruction"
    assert event.after["disposition"] == "qualify" and event.reason
    with pytest.raises(ValidationError, match="Sample changed"):
        change_disposition(
            actor=actor,
            sample_id=sample.pk,
            expected_revision=1,
            disposition="reject",
            reason="Stale browser tab",
        )


@pytest.mark.django_db
def test_client_project_corrections_and_deactivation_preserve_records(staff, receipt_data):
    manager = staff("manager", "project_manager")
    project = receipt_data["project"]
    sample = receive_sample(actor=manager, **receipt_data)
    update_client(
        actor=manager,
        client_id=project.client_id,
        expected_revision=1,
        reason="Correct contact spelling",
        contact_name="Corrected Contact",
    )
    with pytest.raises(ValidationError):
        update_client(
            actor=manager,
            client_id=project.client_id,
            expected_revision=1,
            reason="Stale request",
            name="Wrong",
        )
    update_project(
        actor=manager,
        project_id=project.pk,
        expected_revision=1,
        reason="Project completed",
        is_active=False,
    )
    with pytest.raises(ValidationError, match="inactive"):
        create_order(actor=manager, project=project, coc_reference="BLOCKED")
    other_client = Client.objects.create(name="Other client")
    with pytest.raises(ValidationError, match="cannot change"):
        update_project(
            actor=manager,
            project_id=project.pk,
            expected_revision=2,
            reason="Reassign ownership",
            client=other_client,
        )
    sample.refresh_from_db()
    assert sample.order.project_id == project.pk
    assert Sample.objects.count() == 1 and Project.objects.filter(pk=project.pk).exists()
    assert AuditEvent.objects.get(action="project.updated").before["is_active"] is True
    update_project(
        actor=manager,
        project_id=project.pk,
        expected_revision=2,
        reason="Reopened project",
        is_active=True,
    )
    update_client(
        actor=manager,
        client_id=project.client_id,
        expected_revision=2,
        reason="Client no longer ordering",
        is_active=False,
    )
    with pytest.raises(ValidationError, match="client is inactive"):
        receive_sample(actor=manager, **receipt_data)


@pytest.mark.django_db
def test_order_continuation_http_accepts_formset_without_retyping_coc(client, staff, receipt_data):
    actor = staff("custodian", "custodian")
    client.force_login(actor)
    response = client.post(
        reverse("receipt:order-create"),
        {"project": receipt_data["project"].pk, "coc_reference": "COC-HTTP"},
    )
    assert response.status_code == 302
    order = Order.objects.get(coc_reference="COC-HTTP")
    for revision in [1, 2]:
        data = {
            **sample_inputs(receipt_data),
            "tests": [receipt_data["tests"][0].pk],
            "collection_started_at": "",
            "expected_revision": revision,
            "client_sample_id": f"HTTP-{revision}",
            "containers-TOTAL_FORMS": "2",
            "containers-INITIAL_FORMS": "0",
            "containers-0-description": "Bottle A",
            "containers-0-condition": "Intact",
            "containers-1-description": "Bottle B",
            "containers-1-condition": "Intact",
        }
        response = client.post(reverse("receipt:order-add-sample", kwargs={"pk": order.pk}), data)
        assert response.status_code == 302
        assert response.url == reverse("receipt:order-detail", kwargs={"pk": order.pk})
    assert order.samples.count() == 2
    assert all(s.containers.count() == 2 for s in order.samples.all())


def test_container_formset_enforces_bounds():
    empty = ContainerFormSet(
        {"containers-TOTAL_FORMS": "0", "containers-INITIAL_FORMS": "0"}, prefix="containers"
    )
    assert not empty.is_valid()
    excessive = ContainerFormSet(
        {"containers-TOTAL_FORMS": "10000", "containers-INITIAL_FORMS": "0"}, prefix="containers"
    )
    assert not excessive.is_valid() and len(excessive.forms) == 20


@pytest.mark.django_db(transaction=True)
def test_accession_allocation_is_concurrent_and_does_not_reuse_rollback_numbers():
    from django.db import transaction

    def allocate(_):
        close_old_connections()
        try:
            return allocate_accession()
        finally:
            connections["default"].close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(allocate, range(12)))
    assert len(set(ids)) == 12
    with transaction.atomic():
        rolled_back = allocate_accession()
        transaction.set_rollback(True)
    assert int(allocate_accession()[3:]) > int(rolled_back[3:])


@pytest.mark.django_db
def test_receipt_changes_reject_analyst_role(client, staff, receipt_data):
    custodian = staff("custodian", "custodian")
    sample = receive_sample(actor=custodian, **receipt_data)
    client.force_login(staff("analyst", "analyst"))
    for name, pk in [
        ("order-add-sample", sample.order_id),
        ("sample-disposition", sample.pk),
        ("client-edit", receipt_data["project"].client_id),
        ("project-edit", receipt_data["project"].pk),
    ]:
        response = client.post(reverse(f"receipt:{name}", kwargs={"pk": pk}), {})
        assert response.status_code == 403
    assert client.post(reverse("receipt:order-create"), {}).status_code == 403


@pytest.mark.django_db
def test_collection_location_required_for_new_receipt(staff, receipt_data):
    actor = staff("custodian", "custodian")
    for location in ["", "   "]:
        with pytest.raises(ValidationError) as error:
            receive_sample(actor=actor, **{**receipt_data, "collection_location": location})
        assert "collection_location" in error.value.message_dict
    assert not Sample.objects.exists()
    assert not Order.objects.exists()


def _coc_with_first_sample(client, staff, receipt_data, name="HTTP-DEFAULTS"):
    """Receive one sample over HTTP and return (order, first sample, post-data builder)."""
    actor = staff("custodian", "custodian")
    client.force_login(actor)
    client.post(
        reverse("receipt:order-create"),
        {"project": receipt_data["project"].pk, "coc_reference": name},
    )
    order = Order.objects.get(coc_reference=name)
    url = reverse("receipt:order-add-sample", kwargs={"pk": order.pk})

    def payload(revision, **overrides):
        return {
            **sample_inputs(receipt_data),
            "tests": [receipt_data["tests"][0].pk],
            "collection_started_at": "",
            "expected_revision": revision,
            "containers-TOTAL_FORMS": "1",
            "containers-INITIAL_FORMS": "0",
            "containers-0-description": "Amber bottle",
            "containers-0-condition": "Intact",
            "containers-0-preservation": "HNO3 to pH < 2",
            **overrides,
        }

    return order, url, payload


@pytest.mark.django_db
def test_receive_another_prefills_only_shared_defaults_from_same_order(client, staff, receipt_data):
    order, url, payload = _coc_with_first_sample(client, staff, receipt_data)
    response = client.post(url, {**payload(1), "next": "defaults"})
    first = order.samples.get()
    assert response.status_code == 302
    assert response.url == f"{url}?defaults_from={first.pk}"

    order.refresh_from_db()
    page = client.get(response.url)
    body = page.content.decode()
    form, formset = page.context["form"], page.context["container_formset"]
    assert "Defaults reused from" in body and first.lab_id in body
    assert form.initial["matrix"] == "water"
    assert form.initial["collector_name"] == receipt_data["collector_name"]
    assert form.initial["relinquished_by"] == receipt_data["relinquished_by"]
    assert form.initial["tests"] == [receipt_data["tests"][0].pk]
    assert form.initial["expected_revision"] == order.revision == 2
    assert formset.initial == [{"description": "Amber bottle", "preservation": "HNO3 to pH < 2"}]
    # Observations and identifiers are never copied.
    for name in [
        "client_sample_id",
        "collection_location",
        "collected_at",
        "collection_started_at",
        "received_at",
        "cooler_temperature",
        "temperature_instrument",
        "seal_condition",
        "preservation_check",
        "discrepancy",
        "disposition_note",
        "current_location",
    ]:
        assert name not in form.initial, name
    assert "condition" not in formset.initial[0]
    assert order.samples.count() == 1  # Viewing defaults receives nothing.


@pytest.mark.django_db
def test_receive_another_creates_independent_sample_with_own_accession_and_custody(
    client, staff, receipt_data
):
    order, url, payload = _coc_with_first_sample(client, staff, receipt_data)
    client.post(url, {**payload(1), "next": "defaults"})
    first = order.samples.get()
    second_data = payload(
        2,
        client_sample_id="HTTP-DEFAULTS-2",
        collection_location="Synthetic outfall south bank",
        cooler_temperature="5.250",
    )
    response = client.post(f"{url}?defaults_from={first.pk}", second_data)
    assert response.status_code == 302
    second = order.samples.exclude(pk=first.pk).get()
    assert second.lab_id != first.lab_id and int(second.lab_id[3:]) > int(first.lab_id[3:])
    assert second.collection_location != first.collection_location
    assert second.cooler_temperature != first.cooler_temperature
    assert second.client_sample_id == "HTTP-DEFAULTS-2"
    assert not set(first.containers.values_list("pk", flat=True)) & set(
        second.containers.values_list("pk", flat=True)
    )
    assert second.containers.get().label == f"{second.lab_id}-01"
    assert first.custody_events.count() == second.custody_events.count() == 1
    assert first.custody_events.get().pk != second.custody_events.get().pk
    assert AuditEvent.objects.filter(action="sample.received").count() == 2
    # Plain submit keeps the existing redirect and offers the explicit continuation link.
    follow = client.get(response.url)
    assert f"{url}?defaults_from={second.pk}" in follow.content.decode()


@pytest.mark.django_db
def test_receive_another_incomplete_observations_are_refused_and_input_kept(
    client, staff, receipt_data
):
    order, url, payload = _coc_with_first_sample(client, staff, receipt_data)
    client.post(url, {**payload(1), "next": "defaults"})
    first = order.samples.get()
    data = payload(
        2, client_sample_id="KEEP-ME", seal_condition="", **{"containers-0-condition": ""}
    )
    response = client.post(f"{url}?defaults_from={first.pk}", data)
    assert response.status_code == 200
    assert order.samples.count() == 1
    assert "seal_condition" in response.context["form"].errors
    assert response.context["container_formset"].total_error_count
    assert b"KEEP-ME" in response.content
    assert b"Defaults reused from" in response.content


@pytest.mark.django_db
def test_receive_another_revalidates_tests_and_refuses_stale_resubmit(client, staff, receipt_data):
    order, url, payload = _coc_with_first_sample(client, staff, receipt_data)
    client.post(url, {**payload(1), "next": "defaults"})
    first = order.samples.get()
    test = receipt_data["tests"][0]
    wrong_matrix = payload(2, matrix="soil", client_sample_id="WRONG")
    response = client.post(f"{url}?defaults_from={first.pk}", wrong_matrix)
    assert response.status_code == 200 and "tests" in response.context["form"].errors
    # A retired test is not prefilled and cannot be posted back.
    test.is_active = False
    test.save()
    page = client.get(f"{url}?defaults_from={first.pk}")
    assert page.context["form"].initial["tests"] == []
    assert page.context["defaults"]["skipped_tests"] == 1
    assert b"no longer active" in page.content
    response = client.post(f"{url}?defaults_from={first.pk}", payload(2, client_sample_id="OLD"))
    assert response.status_code == 200 and order.samples.count() == 1
    test.is_active = True
    test.save()
    good = payload(2, client_sample_id="SECOND")
    assert client.post(f"{url}?defaults_from={first.pk}", good).status_code == 302
    # Replaying the same submission (double click, back button) must not receive again.
    again = client.post(f"{url}?defaults_from={first.pk}", good)
    assert again.status_code == 200
    assert "Order changed" in again.content.decode()
    assert order.samples.count() == 2


@pytest.mark.django_db
def test_receive_another_source_must_belong_to_order_and_role_is_enforced(
    client, staff, receipt_data
):
    order, url, payload = _coc_with_first_sample(client, staff, receipt_data)
    client.post(url, {**payload(1), "next": "defaults"})
    other = create_order(
        actor=staff("other_custodian", "custodian"),
        project=receipt_data["project"],
        coc_reference="COC-OTHER",
    )
    other_url = reverse("receipt:order-add-sample", kwargs={"pk": other.pk})
    first = order.samples.get()
    for raw in [str(first.pk), "not-a-uuid"]:
        page = client.get(f"{other_url}?defaults_from={raw}")
        assert page.status_code == 200
        assert page.context["defaults"] is None
        assert not page.context["container_formset"].initial
        assert b'id="reused-defaults"' not in page.content
        assert b"Starting with a blank form" in page.content
    client.force_login(staff("analyst2", "analyst"))
    assert client.get(f"{url}?defaults_from={first.pk}").status_code == 403
    assert order.samples.count() == 1 and not other.samples.exists()
