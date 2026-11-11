from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from analytical.seed import seed_analytical_config


class Command(BaseCommand):
    help = (
        "Seed synthetic demonstration holding rules, QC requirements and limits for active "
        "methods. Not validated limits; replace before real use."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--method",
            action="append",
            dest="methods",
            help="Method code to seed (repeatable). Default: all active methods.",
        )

    def handle(self, *args, **options):
        try:
            count = seed_analytical_config(options["methods"])
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        self.stdout.write(f"Seeded synthetic analytical configuration for {count} method(s).")
