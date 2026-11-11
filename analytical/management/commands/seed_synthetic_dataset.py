from django.core.management.base import BaseCommand

from analytical.synthetic_dataset import seed_synthetic_dataset


class Command(BaseCommand):
    help = (
        "Create the synthetic water, wastewater and soil demonstration methods, analytes, "
        "holding rules and QC configuration where missing. Never overwrites or deletes "
        "existing rows. All values are invented and not validated."
    )

    def handle(self, *args, **options):
        created = seed_synthetic_dataset()
        self.stdout.write(
            "Synthetic dataset ready; created: "
            + ", ".join(f"{count} {name}" for name, count in created.items())
        )
