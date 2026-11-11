from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from analytical import services as analytical
from analytical.models import Result
from analytical.seed import seed_analytical_config
from catalog.models import TestDefinition
from core.models import Lab
from receipt.models import Project, RequestedTest, Sample
from reporting.edd import services as edd
from reporting.models import NysdecProjectSettings, Report
from reporting.services import create_report

DEMO_LAB = "Example Environmental Lab"
DEMO_PROJECT = "Test Creek Outfall 001"


class Command(BaseCommand):
    help = (
        "Take the synthetic demo sample through analysis and review and prepare a draft report. "
        "Run after seed_demo. Idempotent; never releases, resets or edits existing records."
    )

    @transaction.atomic
    def handle(self, *args, **options):
        lab = Lab.objects.filter(pk=1).first()
        if lab is None or lab.name != DEMO_LAB:
            raise CommandError("Refusing to seed outside the synthetic demo laboratory.")
        try:
            analyst, reviewer, manager = (
                User.objects.get(username=f"demo_{role}")
                for role in ("analyst", "reviewer", "project_manager")
            )
            project = Project.objects.get(name=DEMO_PROJECT, client__name="Example Utilities")
        except (User.DoesNotExist, Project.DoesNotExist) as exc:
            raise CommandError("Run seed_demo first.") from exc
        seed_analytical_config()
        tests = RequestedTest.objects.filter(
            sample__order__project=project, result__isnull=True
        ).exclude(sample__disposition__in=["hold", "reject"])
        approved = sum(self._analyse(rt, analyst, reviewer) for rt in tests.order_by("pk"))
        samples = Sample.objects.filter(
            order__project=project, requested_tests__result__status="approved"
        ).distinct()
        if samples and not Report.objects.filter(project=project).exists():
            create_report(
                actor=manager,
                project=project,
                title=f"{DEMO_PROJECT} analytical report",
                samples=samples,
                narrative="Synthetic demonstration report. Values are invented.",
            )
        self._seed_nysdec(project, manager)
        self.stdout.write(
            self.style.SUCCESS(f"Approved {approved} synthetic result(s); draft report ready.")
        )

    def _seed_nysdec(self, project, manager):
        """Obviously fake NYSDEC codes so the exploratory EDD preview can be demonstrated. They are
        not registered NYSDEC values and EQuIS EDP will reject them. Only missing rows are added;
        soil/wastewater codes and soil basis are left unset so the preview shows real gaps."""
        if not NysdecProjectSettings.objects.filter(project=project).exists():
            edd.save_project_settings(
                actor=manager,
                project_id=project.pk,
                data_provider="SYNTH-PROVIDER",
                sampling_company_code="SYNTH-SAMPLER",
                lab_name_code="SYNTH-LAB",
                task_code="SM",
                water_matrix_code="SYNTH-WS",
                water_qc_matrix_code="SYNTH-QC",
            )
        definitions = (
            TestDefinition.objects.filter(
                requestedtest__sample__order__project=project, nysdec_mapping__isnull=True
            )
            .select_related("method", "analyte")
            .distinct()
        )
        for d in definitions:
            edd.save_test_mapping(
                actor=manager,
                definition_id=d.pk,
                lab_anl_method_name=f"SYNTH-{d.method.code}"[:20],
                fraction="SYNTH",
                preservative="SYNTH",
                basis="NA" if d.matrix != "soil" else "",
                chemical_name=f"SYNTH {d.analyte.name}"[:75],
                unit_code=f"SYNTH {d.unit}"[:15],
            )

    def _analyse(self, rt, analyst, reviewer):
        sample, definition = rt.sample, rt.definition
        analysed_at = min(timezone.now(), sample.received_at + timedelta(minutes=30))
        batch = analytical.create_batch(
            actor=analyst, method=definition.method, matrix=definition.matrix
        )
        analytical.add_tests_to_batch(actor=analyst, batch_id=batch.pk, requested_test_ids=[rt.pk])
        limit = (definition.reporting_limit or Decimal(1)).normalize()
        rl = format(limit, "f")
        value = format((limit * Decimal("5.2")).normalize(), "f")  # synthetic, above the RL
        for qc_type, label, text, spike in (
            ("method_blank", "MB-1", "ND", ""),
            ("lcs", "LCS-1", "0.98", "1.00"),
        ):
            qc = analytical.add_qc_sample(
                actor=analyst, batch_id=batch.pk, qc_type=qc_type, label=label
            )
            analytical.record_qc_measurement(
                actor=analyst,
                qc_sample_id=qc.pk,
                definition_id=definition.pk,
                observed_text=text,
                reporting_limit_applied=rl,
                analysis_started_at=analysed_at,
                spike_value=spike,
            )
        result = Result.objects.get(requested_test=rt)
        revision = analytical.enter_result(
            actor=analyst,
            result_id=result.pk,
            supplied_text=value,
            reporting_limit_applied=rl,
            dilution_factor="1",
            analysis_started_at=analysed_at,
        )
        analytical.submit_result(actor=analyst, revision_id=revision.pk)
        analytical.approve_result(actor=reviewer, revision_id=revision.pk, note="Demo review.")
        return 1
