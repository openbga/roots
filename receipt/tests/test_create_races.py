"""A record changed by someone else between form validation and the service: a form error."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connections
from django.urls import reverse

from core.models import AuditEvent
from receipt import views
from receipt.models import Client, Project
from receipt.services import update_client


def in_other_connection(job):
    def run():
        try:
            return job()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(run).result()


@pytest.mark.django_db(transaction=True)
def test_project_for_a_client_deactivated_meanwhile_is_refused_on_the_form(
    client, intake, monkeypatch
):
    pm = intake["user"]("race_pm", "project_manager")
    owner = intake["client"]
    real = views.create_project

    def deactivated_first(**kwargs):
        # The client is deactivated after the form accepted it, before the service locks it.
        in_other_connection(
            lambda: update_client(
                actor=pm,
                client_id=owner.pk,
                expected_revision=Client.objects.get(pk=owner.pk).revision,
                reason="Account closed",
                is_active=False,
            )
        )
        return real(**kwargs)

    monkeypatch.setattr(views, "create_project", deactivated_first)
    client.force_login(pm)
    response = client.post(
        reverse("receipt:project-create"),
        {"client": owner.pk, "name": "Raced project", "turnaround_days": 10},
    )
    assert response.status_code == 200
    assert "inactive" in " ".join(response.context["form"].non_field_errors())
    assert response.context["form"]["name"].value() == "Raced project"
    assert not Project.objects.filter(name="Raced project").exists()
    assert not AuditEvent.objects.filter(action="project.created").exists()
