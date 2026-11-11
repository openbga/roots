import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from catalog.models import TestDefinition
from commercial import pricing, quotes, submissions
from commercial.models import Quote, Submission
from core.models import Lab
from receipt.models import Project

DEMO_LAB = "Example Environmental Lab"  # the name seed_demo creates and requires
NAMESPACE = uuid.UUID("5a1e8a4e-0c1f-4c8e-9a53-7f3f0f6c2d10")
PRICE_LIST = "Standard schedule (synthetic)"


def token(name):
    return uuid.uuid5(NAMESPACE, name)


class Command(BaseCommand):
    help = (
        "Add synthetic price list, quotes and client submissions to the demo laboratory. "
        "Run after seed_demo; existing records are never changed."
    )

    @transaction.atomic
    def handle(self, *args, **options):
        # Same guard as seed_demo: synthetic records only ever go into the demo laboratory.
        lab = Lab.objects.filter(pk=1).first()
        if lab is None or lab.name != DEMO_LAB:
            raise CommandError("Refusing to seed a non-demo laboratory; run seed_demo first.")
        try:
            pm = User.objects.get(username="demo_project_manager")
            client_user = User.objects.get(username="demo_client")
            project = Project.objects.get(client=client_user.client, name="Test Creek Outfall 001")
        except (User.DoesNotExist, Project.DoesNotExist) as exc:
            raise CommandError(
                "Run seed_demo first; it creates the demo users and project."
            ) from exc
        tests = list(
            TestDefinition.objects.filter(is_active=True, method__is_active=True)
            .select_related("method", "analyte")
            .order_by("matrix", "analyte__name", "pk")
        )
        if not tests:
            raise CommandError("The catalog has no active tests; run seed_demo first.")
        price_list = self.price_list(pm, tests)
        water = [t for t in tests if t.matrix == "water"][:4]
        soil = [t for t in tests if t.matrix == "soil"][:2]
        if not water:
            raise CommandError("The demo catalog has no active water tests.")
        self.quotes(pm, client_user, project, price_list, water, soil)
        self.submissions(client_user, project, water, soil)
        self.stdout.write("Synthetic commercial records are in place.")

    def price_list(self, pm, tests):
        from commercial.models import PriceList

        existing = PriceList.objects.filter(name=PRICE_LIST).first()
        if existing:
            return existing
        # Deterministic invented prices: not a real schedule.
        prices = {
            t.pk: Decimal("14.00")
            + Decimal("3.25") * (i % 9)
            + (Decimal("6.00") if t.matrix == "soil" else 0)
            for i, t in enumerate(tests)
        }
        return pricing.save_price_list(
            actor=pm,
            name=PRICE_LIST,
            description="Invented prices for demonstration only.",
            prices=prices,
        )

    def quotes(self, pm, client_user, project, price_list, water, soil):
        valid_until = timezone.localdate() + timedelta(days=45)

        def build(name, title, finish):
            if Quote.objects.filter(token=token(name)).exists():
                return
            quote = quotes.create_quote(
                actor=pm, token=token(name), client=project.client, project=project, title=title
            )
            lines = [{"kind": "test", "test": t, "quantity": 6} for t in water] + [
                {
                    "kind": "fee",
                    "description": "Sample kits and courier (per quarter)",
                    "quantity": 1,
                    "unit_price": Decimal("85.00"),
                },
            ]
            if soil:
                lines += [{"kind": "test", "test": t, "quantity": 2} for t in soil]
            lines.append(
                {
                    "kind": "adjustment",
                    "description": "Annual monitoring agreement discount",
                    "quantity": 1,
                    "unit_price": Decimal("-40.00"),
                }
            )
            quotes.save_draft(
                actor=pm,
                quote_id=quote.pk,
                expected_revision=quote.revision,
                title=title,
                project=project,
                price_list=price_list,
                valid_until=valid_until,
                turnaround_days=10,
                scope_notes="Quarterly outfall monitoring: six grab samples per quarter.",
                terms="Net 30. Prices in US dollars. Rush work needs prior laboratory approval.",
                lines=lines,
            )
            quote.refresh_from_db()
            finish(quote)

        def issue(quote):
            quotes.issue_quote(
                actor=pm, quote_id=quote.pk, version_number=1, expected_revision=quote.revision
            )

        def accept(quote):
            issue(quote)
            quotes.accept_quote(
                actor=client_user,
                quote_id=quote.pk,
                version_number=1,
                client_reference="PO-DEMO-2041",
            )

        build("quote-accepted", "2026 quarterly outfall monitoring", accept)
        build("quote-issued", "Spring sludge and effluent characterization", issue)
        build("quote-draft", "Storm event sampling (draft)", lambda quote: None)

    def submissions(self, client_user, project, water, soil):
        def sample(sub, ident, matrix, tests, hours_ago, **extra):
            data = dict(
                client_sample_id=ident,
                matrix=matrix,
                collection_type="grab",
                collection_started_at=None,
                collected_at=timezone.now() - timedelta(hours=hours_ago),
                collector_name="Synthetic Sampler",
                collection_location="Test Creek Outfall 001",
                field_notes="",
            )
            data.update(extra)
            containers = [
                {"description": "250 mL HDPE", "preservation": "HNO3 to pH < 2", "quantity": 1},
                {
                    "description": "1 L amber glass",
                    "preservation": "Cool to 6 °C or below",
                    "quantity": 1,
                },
            ]
            if matrix == "soil":
                containers = [
                    {"description": "8 oz glass jar", "preservation": "Cool", "quantity": 1}
                ]
            submissions.save_sample(
                actor=client_user,
                submission_id=sub.pk,
                expected_revision=Submission.objects.get(pk=sub.pk).revision,
                tests=tests,
                containers=containers,
                **data,
            )

        def build(name, coc, finish):
            if Submission.objects.filter(token=token(name)).exists():
                return
            sub = submissions.create_submission(
                actor=client_user,
                token=token(name),
                project=project,
                coc_reference=coc,
                expected_arrival=timezone.localdate() + timedelta(days=1),
                carrier="Overnight courier, tracking synthetic",
                cooler_count=1,
                contact_name="Synthetic Contact",
                contact_phone="555-0100",
                special_instructions="Synthetic demonstration shipment.",
            )
            sample(sub, "TC-OF001-A", "water", water, 20)
            sample(
                sub,
                "TC-OF001-B",
                "water",
                water[:2],
                20,
                collection_type="composite",
                collection_started_at=timezone.now() - timedelta(hours=44),
                collected_at=timezone.now() - timedelta(hours=20),
            )
            if soil:
                sample(sub, "TC-SOIL-01", "soil", soil, 26, collection_location="Test Creek bank")
            sub.refresh_from_db()
            finish(sub)

        def submit(sub):
            submissions.submit_submission(
                actor=client_user, submission_id=sub.pk, expected_revision=sub.revision
            )

        def returned(sub):
            submit(sub)
            submissions.return_submission(
                actor=User.objects.get(username="demo_custodian"),
                submission_id=sub.pk,
                snapshot_number=1,
                reason="Add the sampling time for the composite end.",
            )

        build("sub-submitted", "TC-COC-2026-0301", submit)
        build("sub-returned", "TC-COC-2026-0302", returned)
        build(
            "sub-draft",
            "TC-COC-2026-0303",
            lambda sub: sample(
                sub,
                "TC-OF001-PLAN",
                "water",
                water[:2],
                0,
                collected_at=None,
                planned_collection_on=timezone.localdate() + timedelta(days=3),
            ),  # bottles being prepared: not collected yet
        )
