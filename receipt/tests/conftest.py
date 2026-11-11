from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone

from accounts.models import User
from catalog.models import Analyte, Method, TestDefinition
from receipt.models import Client, Project
from receipt.services import receive_sample


@pytest.fixture
def intake(db):
    def user(name, role=None, client=None):
        obj = User.objects.create(username=name, email=f"{name}@example.invalid", client=client)
        obj.set_unusable_password()
        obj.save()
        if role:
            obj.groups.add(Group.objects.get_or_create(name=role)[0])
        return obj

    actor = user("receipt_p2", "custodian")
    owner = Client.objects.create(name="Synthetic Client")
    project = Project.objects.create(client=owner, name="Synthetic Project", turnaround_days=10)
    method = Method.objects.create(code="DEMO-P2", version="1", title="Synthetic method")
    definition = TestDefinition.objects.create(
        method=method,
        analyte=Analyte.objects.create(name="Synthetic analyte"),
        matrix="water",
        unit="mg/L",
    )

    def sample(**overrides):
        now = timezone.now() - timedelta(minutes=1)
        values = dict(
            project=project,
            coc_reference="COC-P2",
            tests=[definition],
            container_description="Bottle",
            container_condition="Intact",
            relinquished_by="Courier",
            client_sample_id="FIELD-1",
            matrix="water",
            collection_type="grab",
            collected_at=now - timedelta(hours=1),
            collector_name="Synthetic collector",
            collection_location="Synthetic north bank",
            received_at=now,
            cooler_temperature=Decimal("4.125"),
            seal_condition="Intact",
            preservation_check="Checked",
            discrepancy="",
            disposition="proceed",
            disposition_note="",
            current_location="Bench",
        )
        return receive_sample(actor=actor, **(values | overrides))

    def configure_preservation(*definitions):
        """Explicit synthetic rule for tests that approve or release work (the gate is not
        bypassed). The default is no rule, which the gate treats as unknown."""
        from analytical.models import PreservationRule

        for item in definitions or [definition]:
            PreservationRule.objects.get_or_create(
                definition=item,
                defaults={
                    "temperature_mode": "not_required",
                    "basis": "SYNTHETIC TEST: no temperature requirement; not method guidance.",
                },
            )

    return {
        "configure_preservation": configure_preservation,
        "actor": actor,
        "client": owner,
        "project": project,
        "method": method,
        "definition": definition,
        "sample": sample,
        "user": user,
    }
