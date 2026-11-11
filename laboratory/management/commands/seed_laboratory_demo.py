"""Synthetic laboratory-operations demo records (materials, equipment, training, nonconformance).

Idempotent: does nothing when the synthetic materials already exist. Uses existing staff accounts
(one with the admin or reviewer role as the acting user); it creates no users, so run it after the
demo roles exist. Like the other demo seeds it refuses any laboratory but the demo one created by
seed_demo, and writes everything in one transaction or nothing.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from catalog.models import Method
from core.models import Lab
from laboratory import equipment, inventory, training
from laboratory import nonconformance as nc
from laboratory.models import Material

DEMO_LAB = "Example Environmental Lab"  # the name seed_demo creates and requires


class Command(BaseCommand):
    help = "Create synthetic inventory, equipment, training and nonconformance demo records."

    @transaction.atomic
    def handle(self, *args, **options):
        # Same guard as seed_demo: synthetic records only ever go into the demo laboratory.
        lab = Lab.objects.filter(pk=1).first()
        if lab is None or lab.name != DEMO_LAB:
            raise CommandError("Refusing to seed a non-demo laboratory; run seed_demo first.")
        if Material.objects.filter(name__startswith="Demo ").exists():
            self.stdout.write("Laboratory demo records already exist; nothing to do.")
            return
        lead = User.objects.filter(
            is_active=True, client__isnull=True, groups__name__in=["admin", "reviewer"]
        ).first()
        analyst = User.objects.filter(
            is_active=True, client__isnull=True, groups__name="analyst"
        ).first()
        if not (lead and analyst):
            raise CommandError("Create staff with the admin or reviewer role and an analyst first.")
        today = timezone.localdate()
        day = lambda n: today + timedelta(days=n)  # noqa: E731

        acid = inventory.save_material(
            actor=lead,
            name="Demo nitric acid, trace-metal grade",
            kind="reagent",
            unit="mL",
            specification="Synthetic specification",
            storage_requirements="Acid cabinet",
        )
        std = inventory.save_material(
            actor=lead,
            name="Demo multi-element standard",
            kind="standard",
            unit="mL",
            storage_requirements="Refrigerator",
        )
        common = dict(actor=analyst, source="purchased", vendor="Demo Supply Co")
        inventory.receive_lot(
            material=acid,
            lot_number="DEMO-A1",
            received_on=day(-30),
            expires_on=day(300),
            storage_location="Cabinet A",
            quantity=Decimal("2500"),
            **common,
        )
        inventory.receive_lot(
            material=std,
            lot_number="DEMO-S1",
            received_on=day(-120),
            expires_on=day(20),
            storage_location="Refrigerator 1",
            quantity=Decimal("40.5"),
            stated_concentration="10 mg/L each element (as labeled)",
            **common,
        )
        inventory.receive_lot(
            material=std,
            lot_number="DEMO-S0",
            received_on=day(-400),
            expires_on=day(-10),
            storage_location="Refrigerator 1",
            quantity=Decimal("5"),
            **common,
        )

        balance = equipment.save_equipment(
            actor=lead,
            asset_id="DEMO-BAL-01",
            name="Analytical balance",
            equipment_type="balance",
            manufacturer="Demo Instruments",
            model="DB-200",
            serial_number="DEMO-0001",
            location="Weigh room",
            notes="",
        )
        icp = equipment.save_equipment(
            actor=lead,
            asset_id="DEMO-ICP-01",
            name="ICP-OES",
            equipment_type="ICP",
            manufacturer="Demo Instruments",
            model="DI-7000",
            serial_number="DEMO-0002",
            location="Instrument lab",
            notes="",
        )
        for asset, kind, outcome, ago, due in [
            (balance, "calibration", "acceptable", 340, 25),
            (balance, "maintenance", "completed", 200, -5),
            (icp, "calibration", "acceptable", 60, None),
        ]:
            equipment.record_equipment_event(
                actor=analyst,
                equipment_id=asset.pk,
                kind=kind,
                performed_on=day(-ago),
                performed_by=analyst,
                outcome=outcome,
                reference=f"DEMO-{kind[:3].upper()}-{ago}",
                next_due_on=day(due) if due is not None else None,
            )

        method = Method.objects.filter(is_active=True).first()
        if method:
            for exp, title in [(day(25), "Initial DOC (demo)"), (day(-15), "Ongoing DOC (demo)")]:
                training.record_training(
                    actor=lead,
                    staff=analyst,
                    record_type="doc_initial",
                    title=title,
                    method=method,
                    matrix="water",
                    completed_on=day(-340),
                    effective_on=day(-340),
                    expires_on=exp,
                    reviewed_by=lead,
                    reviewed_on=day(-339),
                    reference="DEMO-DOC",
                )
        training.record_training(
            actor=lead,
            staff=analyst,
            record_type="training",
            title="Demo safety orientation",
            completed_on=day(-90),
            effective_on=day(-90),
            reviewed_by=lead,
            reviewed_on=day(-90),
        )

        issue = nc.open_nonconformance(
            actor=analyst,
            title="Demo: standard past expiry found in use area",
            description="Synthetic example of an expired standard on the bench.",
            severity="minor",
            discovered_on=day(-3),
            category="material",
            due_on=day(4),
            lot=inventory.MaterialLot.objects.get(lot_number="DEMO-S0"),
        )
        nc.transition(
            actor=lead,
            nc_id=issue.pk,
            expected_revision=issue.revision,
            to_status="investigating",
            owner=analyst,
        )
        self.stdout.write(self.style.SUCCESS("Laboratory demo records created."))
