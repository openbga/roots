import os
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from accounts.roles import ROLES
from analytical.synthetic_dataset import seed_synthetic_dataset
from catalog.models import TestDefinition
from core.models import Lab
from receipt.models import Client, Project, Sample
from receipt.services import receive_sample


class Command(BaseCommand):
    help = "Create synthetic demo records once; never reset existing data or passwords."

    @transaction.atomic
    def handle(self, *args, **options):
        password = os.environ.get("DEMO_PASSWORD")
        if not password or len(password) < 12:
            raise CommandError("Set DEMO_PASSWORD to at least 12 characters; it is never printed.")
        lab, created = Lab.objects.get_or_create(
            pk=1, defaults={"name": "Example Environmental Lab"}
        )
        if not created and lab.name != "Example Environmental Lab":
            raise CommandError("Refusing to seed a non-demo laboratory.")
        for role in ROLES:
            group, _ = Group.objects.get_or_create(name=role)
            user, new = User.objects.get_or_create(
                username=f"demo_{role}",
                defaults={
                    "email": f"{role}@example.invalid",
                    "first_name": "Demo",
                    "last_name": role,
                    "is_staff": role == "admin",
                },
            )
            if new:
                user.set_password(password)
                user.save()
                user.groups.add(group)
                if role == "approver":
                    user.user_permissions.add(
                        Permission.objects.get(
                            content_type__app_label="accounts", codename="authorize_release"
                        )
                    )
        client, _ = Client.objects.get_or_create(
            name="Example Utilities",
            defaults={
                "account_number": "EXUTIL",
                "contact_name": "Synthetic Contact",
                "email": "contact@example.invalid",
                "phone": "555-0100",
                "address_line1": "100 Example Street",
                "city": "Exampleville",
                "state": "NY",
                "postal_code": "00000",
                "billing_contact_name": "Synthetic Accounts Payable",
                "billing_email": "payables@example.invalid",
                "payment_terms": "net_30",
                "report_preferences": "PDF report to the primary contact. Synthetic.",
            },
        )
        project, _ = Project.objects.get_or_create(
            client=client,
            name="Test Creek Outfall 001",
            defaults={
                "number": "EX-0001",
                "regulatory_program": "npdes",
                "program": "Synthetic training only",
                "site_address": "Synthetic Test Creek treatment plant",
                "site_state": "NY",
                "project_manager": User.objects.get(username="demo_project_manager"),
                "instructions": "Synthetic standing instruction: check sample IDs against labels.",
            },
        )
        client_user, new = User.objects.get_or_create(
            username="demo_client", defaults={"email": "client@example.invalid", "client": client}
        )
        if new:
            client_user.set_password(password)
            client_user.save()
        seed_synthetic_dataset()
        water_tests = list(
            TestDefinition.objects.filter(
                method__code="DEMO-METALS-AQ", method__version="demo-1", matrix="water"
            ).order_by("pk")
        )
        if not Sample.objects.filter(order__project=project).exists():
            now = timezone.now()
            receive_sample(
                actor=User.objects.get(username="demo_custodian"),
                project=project,
                coc_reference="DEMO-COC-001",
                tests=water_tests,
                container_description="500 mL plastic (HDPE)",
                container_condition="Intact",
                relinquished_by="Example Courier",
                client_sample_id="TEST-001",
                matrix="water",
                source_type="surface_water",
                collection_type="grab",
                collected_at=now - timedelta(hours=2),
                collection_started_at=None,
                collector_name="Example Collector",
                collection_location="Synthetic Test Creek outfall 001",
                received_at=now,
                cooler_temperature=Decimal("4.200"),
                coolant="wet_ice",
                seal_condition="Intact",
                preservation_check="Recorded for demonstration only",
                discrepancy="",
                disposition="proceed",
                disposition_note="",
                current_location="Receipt bench",
            )
        self.stdout.write(
            self.style.SUCCESS("Synthetic demo ready; existing records/passwords retained.")
        )
