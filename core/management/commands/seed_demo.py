import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from accounts.roles import ROLES
from analytical.synthetic_dataset import seed_synthetic_dataset
from catalog.models import TestDefinition
from core import lab_profile
from core.announcements import save_announcement
from core.models import Announcement, AuditEvent, Lab, LabCertificate
from receipt.models import Client, Project, Sample
from receipt.services import receive_sample

DEMO_LAB_NAME = "Example Environmental Lab"
# An invented emblem for the demonstration laboratory's reports; not the product's mark.
DEMO_LAB_LOGO = Path(__file__).resolve().parents[2] / "demo_assets" / "example-lab-logo.png"
DEMO_LAB_IDENTITY = {
    "address_line1": "200 Example Way",
    "address_line2": "Building S (synthetic)",
    "city": "Exampleville",
    "state": "NY",
    "postal_code": "00000",
    "phone": "555-0142",
    "email": "laboratory@example.invalid",
    "website": "https://laboratory.example.invalid",
}
# (issuing authority, program or scope, certificate or laboratory ID, has validity dates)
DEMO_LAB_CERTIFICATES = (
    ("Example State Department of Health", "Potable and non-potable water", "EX-00000", True),
    ("Example Accreditation Body", "Solid and chemical materials", "EX-ACC-0000", False),
)
ACCOUNTS_ANNOUNCEMENT = "Demo accounts"
# Listed on the sign-in page by --announce-accounts. demo_admin manages announcements and
# accounts, so it is never listed and must not share the published password.
ANNOUNCED_ACCOUNTS = (
    ("demo_custodian", "sample receipt and custody"),
    ("demo_analyst", "batches, results and QC"),
    ("demo_reviewer", "technical review"),
    ("demo_approver", "report release"),
    ("demo_project_manager", "clients, projects, quotes and reports"),
    ("demo_client", "the client portal"),
)


class Command(BaseCommand):
    help = "Create synthetic demo records once; never reset existing data or passwords."

    def add_arguments(self, parser):
        parser.add_argument(
            "--full",
            action="store_true",
            help="Also create a synthetic working week: a second client, a few dozen samples in "
            "every state, batches with QC, control-chart history, an MDL study, draft, released "
            "and amended reports, and the commercial and laboratory demo records.",
        )
        parser.add_argument(
            "--announce-accounts",
            action="store_true",
            help="For a public demonstration: also post a sign-in page announcement that lists "
            "the demo accounts and DEMO_PASSWORD, where anyone can read them, and stop those "
            "accounts changing their own credentials. demo_admin is left out and needs its own "
            "password: set DEMO_ADMIN_PASSWORD before the first seed. DEMO_RESET_NOTE, if set, "
            "ends the announcement (for example when a scheduled reset_demo runs).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        password = os.environ.get("DEMO_PASSWORD")
        if not password or len(password) < 12:
            raise CommandError("Set DEMO_PASSWORD to at least 12 characters; it is never printed.")
        admin_password = os.environ.get("DEMO_ADMIN_PASSWORD") or password
        if len(admin_password) < 12:
            raise CommandError(
                "DEMO_ADMIN_PASSWORD needs at least 12 characters, or leave it unset."
            )
        lab, created = Lab.objects.get_or_create(pk=1, defaults={"name": DEMO_LAB_NAME})
        if not created and lab.name != DEMO_LAB_NAME:
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
                user.set_password(admin_password if role == "admin" else password)
                user.save()
                user.groups.add(group)
                if role == "approver":
                    user.user_permissions.add(
                        Permission.objects.get(
                            content_type__app_label="accounts", codename="authorize_release"
                        )
                    )
        self._seed_profile()
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
        if options.get("announce_accounts"):
            posted = self._announce_accounts(password)
            self.stdout.write(
                "Demo accounts announced on the sign-in page."
                if posted
                else "Demo accounts announcement already present; left as it is."
            )
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
        if options.get("full"):
            from ._demo_full import seed_full_demo  # loads reporting/PDF code only when asked

            created = seed_full_demo(stdout=self.stdout, verbosity=options["verbosity"])
            self.stdout.write(
                "Synthetic working week created."
                if created
                else "Synthetic working week already present; nothing added."
            )
        self.stdout.write(
            self.style.SUCCESS("Synthetic demo ready; existing records/passwords retained.")
        )

    def _seed_profile(self):
        """Give the demonstration laboratory an invented identity for its reports, once.

        A profile that was ever changed, by an earlier seed or by an administrator, is left as
        it is. The certificates are invented and marked for printing so demo reports show them.
        """
        admin = User.objects.get(username="demo_admin")
        if AuditEvent.objects.filter(
            action__in=lab_profile.PROFILE_ACTIONS
        ).exists() or not lab_profile.can_manage(admin):
            return
        lab_profile.save_identity(actor=admin, values=DEMO_LAB_IDENTITY)
        lab_profile.set_logo(
            actor=admin,
            uploaded=SimpleUploadedFile(DEMO_LAB_LOGO.name, DEMO_LAB_LOGO.read_bytes()),
        )
        year = timezone.localdate().year
        for position, (authority, program, number, dated) in enumerate(DEMO_LAB_CERTIFICATES, 1):
            lab_profile.save_certificate(
                actor=admin,
                instance=LabCertificate(
                    authority=authority,
                    program=program,
                    number=number,
                    valid_from=date(year, 1, 1) if dated else None,
                    valid_until=date(year + 1, 12, 31) if dated else None,
                    show_on_reports=True,
                    position=position,
                ),
            )

    def _announce_accounts(self, password):
        """Lock the accounts DEMO_PASSWORD opens and post them on the sign-in page, once.

        Never demo_admin: it manages announcements and accounts.
        """
        admin = User.objects.get(username="demo_admin")
        if admin.check_password(password):
            raise CommandError(
                "Refusing to publish DEMO_PASSWORD: it also opens demo_admin, which manages "
                "announcements and accounts. Give demo_admin its own password (set "
                "DEMO_ADMIN_PASSWORD before the first seed, or change its password) and run "
                "this again. Nothing was saved."
            )
        users = User.objects.in_bulk(
            [name for name, _ in ANNOUNCED_ACCOUNTS], field_name="username"
        )
        shared = [
            (name, work)
            for name, work in ANNOUNCED_ACCOUNTS
            if name in users and users[name].check_password(password)
        ]
        if not shared:
            raise CommandError(
                "No demo account uses DEMO_PASSWORD, so there is nothing to announce. "
                "Nothing was saved."
            )
        # Anyone can sign in to these accounts, so none of them may change its own credentials.
        User.objects.filter(username__in=[name for name, _ in shared]).update(
            credentials_locked=True
        )
        if Announcement.objects.filter(title=ACCOUNTS_ANNOUNCEMENT).exists():
            return False  # posted before; an administrator may have changed or withdrawn it
        lines = [f"{name}: {work}" for name, work in shared]
        # A deployment with a scheduled reset_demo says when (render.demo.yaml sets this).
        reset_note = (
            os.environ.get("DEMO_RESET_NOTE", "").strip()
            or "The demonstration can be reset at any time."
        )
        body = "\n".join(
            [
                "Public demonstration with synthetic data. Sign in as:",
                "",
                *lines,
                "",
                f"Password for each: {password}",
                "",
                "Do not enter real data. Anyone can use these accounts. " + reset_note,
            ]
        )
        try:
            save_announcement(
                actor=admin,
                instance=Announcement(title=ACCOUNTS_ANNOUNCEMENT, body=body, show_on_sign_in=True),
            )
        except PermissionDenied as exc:
            raise CommandError(
                "demo_admin no longer holds the admin role, so it cannot post the announcement. "
                "Nothing was saved."
            ) from exc
        except ValidationError as exc:
            raise CommandError(
                f"The demo accounts announcement was refused: {' '.join(exc.messages)} "
                "Nothing was saved."
            ) from exc
        return True
