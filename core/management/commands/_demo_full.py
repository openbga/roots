"""The synthetic working week behind ``seed_demo --full``.

Everything here is invented demonstration data: two example clients, a few dozen samples over
the last weeks and the laboratory work on them, made only through the domain services the
application itself uses, so every record carries its checks, custody, audit events and history.
Nothing is a real result, person, place, permit or limit.

Idempotent: the work is created once, recognised by its chain-of-custody prefix, and skipped
afterwards. The caller runs it inside one transaction, so a failure leaves nothing behind.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal
from itertools import cycle

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from accounts.models import User
from accounts.roles import can_authorize_release
from analytical import mdl_services, preparation
from analytical import services as bench
from analytical.models import PreparationRequirement, Result, ResultRevision
from catalog.models import Method, TestDefinition
from laboratory.models import Equipment
from receipt import services as receipt
from receipt.models import Client, Order, Project, RequestedTest
from reporting.content import assemble
from reporting.models import Report
from reporting.pdf import ReportRenderError
from reporting.services import create_report, release_report

COC_PREFIX = "SYN-COC-"  # marks the synthetic working week; present means already seeded
VERSION = "demo-1"
UTILITIES = "Example Utilities"
OUTFALL = "Test Creek Outfall 001"
SEWER = "Example County Sewer District"

# Invented observations, cycled per matrix and analyte. A trailing "J" enters the value with the
# demonstration J qualifier (below the reporting limit). Units are the test definition's own.
VALUES = {
    "water": {
        "Lead": ["2.4", "3.1", "ND", "5.6", "1.8", "4.2", "0.7J", "2.9"],
        "Copper": ["12.4", "8.6", "15.1", "21.7", "9.9", "11.3", "6.4", "14.8"],
        "Zinc": ["48", "36", "61", "ND", "55", "42", "39", "51"],
    },
    "wastewater": {
        "Lead": ["8.2", "3.6", "12.4", "6.1", "4.8", "9.7"],
        "Copper": ["64.0", "28.5", "91.2", "45.3", "37.9", "70.6"],
        "Zinc": ["210", "96", "305", "148", "122", "187"],
    },
    "soil": {
        "Lead": ["18.5", "42.0", "9.6", "27.3", "15.2"],
        "Copper": ["21.4", "35.8", "14.1", "26.0", "19.7"],
        "Zinc": ["77.0", "120", "64.2", "95.5", "81.3"],
    },
}
# LCS recoveries scatter around 100 % (invented); the QC-failure batch overrides one.
LCS_AQUEOUS = ["19.6", "20.3", "19.8", "20.2", "19.4", "20.6", "19.9", "20.1", "19.5", "20.4"]
LCS_SOIL = ["48.6", "50.9", "49.3", "51.2"]
SPIKE = {"water": "20.0", "wastewater": "20.0", "soil": "50.0"}
MDL_SPIKES = [  # (days ago, preparation batch, observed); spiking level 2.00
    (20, "MDL-A", "1.82"),
    (20, "MDL-A", "2.11"),
    (20, "MDL-A", "1.95"),
    (13, "MDL-B", "2.04"),
    (13, "MDL-B", "1.88"),
    (6, "MDL-C", "2.17"),
    (6, "MDL-C", "1.97"),
]


def _rl(definition):
    return format((definition.reporting_limit or Decimal(1)).normalize(), "f")


def _fail(exc):
    messages = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
    if exc.__cause__ is not None:  # for example the missing library behind a PDF render error
        messages.append(f"({exc.__cause__})")
    return CommandError("Full demo seeding stopped; nothing was saved: " + " ".join(messages))


def seed_full_demo(*, stdout, verbosity=1):
    """Add the synthetic working week to the demo laboratory (after the basic demo seed)."""
    pending = not Order.objects.filter(coc_reference__startswith=COC_PREFIX).exists()
    if pending:
        approver = User.objects.get(username="demo_approver")
        if not can_authorize_release(approver):
            raise CommandError(
                "demo_approver does not hold release permission, which --full needs to issue "
                "the demo reports. Nothing was saved."
            )
    for command in ("seed_reporting_demo", "seed_commercial_demo", "seed_laboratory_demo"):
        call_command(command, stdout=stdout, verbosity=verbosity)
    try:
        week = Week()
        week.clients()
        if pending:
            week.run()
    except (ValidationError, PermissionDenied, ReportRenderError) as exc:
        raise _fail(exc) from exc
    return pending


class Week:
    def __init__(self):
        users = {u.username: u for u in User.objects.filter(username__startswith="demo_")}
        try:
            self.custodian = users["demo_custodian"]
            self.analyst = users["demo_analyst"]
            self.reviewer = users["demo_reviewer"]
            self.manager = users["demo_project_manager"]
            self.approver = users["demo_approver"]
        except KeyError as exc:
            raise CommandError(f"Demo account {exc} is missing.") from exc
        self.now = timezone.now()
        self.today = timezone.localdate(self.now)
        self.icp = (
            Equipment.objects.filter(asset_id="DEMO-ICP-01")
            .exclude(status=Equipment.Status.RETIRED)
            .first()
        )
        self._values = {}
        self._lcs = {}
        self._coc = 0

    # ------------------------------------------------------------------ time and catalog

    def at(self, days_ago, hour, minute=0):
        """A wall-clock time in the laboratory zone ``days_ago`` days before today."""
        day = self.today - timedelta(days=days_ago)
        return timezone.make_aware(datetime.combine(day, time(hour, minute)))

    def ago(self, hours=0, minutes=0):
        return self.now - timedelta(hours=hours, minutes=minutes)

    def tests(self, code, matrix):
        found = list(
            TestDefinition.objects.filter(
                method__code=code,
                method__version=VERSION,
                matrix=matrix,
                is_active=True,
                method__is_active=True,
            )
            .select_related("analyte", "method")
            .order_by("analyte__name")
        )
        if not found:
            raise CommandError(f"The demo catalog has no active {code} {matrix} tests.")
        return found

    def value(self, definition):
        key = (definition.matrix, definition.analyte.name)
        if key not in self._values:
            self._values[key] = cycle(VALUES[key[0]].get(key[1], ["5.2"]))
        text = next(self._values[key])
        return (text[:-1], ("J",)) if text.endswith("J") else (text, ())

    def lcs(self, definition):
        key = (definition.matrix, definition.analyte.name)
        if key not in self._lcs:
            self._lcs[key] = cycle(LCS_SOIL if definition.matrix == "soil" else LCS_AQUEOUS)
        return next(self._lcs[key])

    # ------------------------------------------------------------------ clients

    def clients(self):
        """The second client and the extra projects, created once through the services."""
        utilities = Client.objects.get(name=UTILITIES)
        self.outfall = Project.objects.get(client=utilities, name=OUTFALL)
        self.river = self.project(
            utilities,
            "Test Creek Receiving Water",
            number="EX-0002",
            regulatory_program="none",
            program="Synthetic training only",
            site_address="Synthetic Test Creek station RW-2",
            site_state="NY",
            instructions="Synthetic standing instruction: one grab sample per weekday at RW-2.",
        )
        sewer = Client.objects.filter(name=SEWER).first() or receipt.create_client(
            actor=self.manager,
            name=SEWER,
            account_number="EXCSD",
            contact_name="Synthetic Plant Operator",
            email="operations@example.invalid",
            phone="555-0140",
            address_line1="200 Example Avenue",
            city="Exampleville",
            state="NY",
            postal_code="00000",
            billing_contact_name="Synthetic Finance Office",
            billing_email="finance@example.invalid",
            payment_terms="net_45",
            po_required=True,
            report_preferences="PDF report to the plant operator. Synthetic.",
            notes="Synthetic demonstration client.",
        )
        self.plant = self.project(
            sewer,
            "Plant 2 Influent and Effluent",
            number="CSD-P2",
            regulatory_program="pretreatment",
            program="Synthetic training only",
            site_address="Synthetic Plant 2, Example County",
            site_state="NY",
            po_number="PO-DEMO-3100",
            instructions="Synthetic standing instruction: influent is a 24-hour composite.",
        )
        self.beds = self.project(
            sewer,
            "Drying Beds Soil Survey",
            number="CSD-DB",
            regulatory_program="none",
            program="Synthetic training only",
            site_address="Synthetic Plant 2 drying beds",
            site_state="NY",
            po_number="PO-DEMO-3101",
            instructions="Synthetic standing instruction: report soil results on a dry basis.",
        )

    def project(self, client, name, **data):
        existing = Project.objects.filter(client=client, name=name).first()
        if existing:
            return existing
        return receipt.create_project(
            actor=self.manager,
            client=client,
            name=name,
            project_manager=self.manager,
            turnaround_days=10,
            **data,
        )

    # ------------------------------------------------------------------ receipt

    def order(self, project, received_at, *, priority="standard", delivery="lab_courier"):
        self._coc += 1
        days = 3 if priority == "rush" else project.turnaround_days
        due_day = timezone.localdate(received_at) + timedelta(days=days)
        return receipt.create_order(
            actor=self.custodian,
            project=project,
            coc_reference=f"{COC_PREFIX}{self._coc:04d}",
            due_at=timezone.make_aware(datetime.combine(due_day, time(17, 0))),
            priority=priority,
            delivery_method=delivery,
            cooler_count=1,
            instructions="Synthetic shipment for the demonstration laboratory.",
        )

    def receive(
        self,
        order,
        ident,
        tests,
        *,
        source,
        received_at,
        collected_at=None,
        composite_hours=None,
        temperature="4.2",
        disposition="proceed",
        note="",
        discrepancy="",
        location="",
        collector="Example County Field Crew",
    ):
        matrix = tests[0].matrix
        collected_at = collected_at or received_at - timedelta(hours=2)
        container = {
            "soil": ("Synthetic 8 oz glass jar", "Cool"),
            "water": ("Synthetic 500 mL HDPE", "HNO3 to pH < 2"),
            "wastewater": ("Synthetic 1 L HDPE", "HNO3 to pH < 2"),
        }[matrix]
        sample = receipt.receive_order_sample(
            actor=self.custodian,
            order_id=order.pk,
            expected_revision=Order.objects.values_list("revision", flat=True).get(pk=order.pk),
            tests=tests,
            containers=[
                {"description": container[0], "condition": "Intact", "preservation": container[1]}
            ],
            relinquished_by="Example Courier",
            client_sample_id=ident,
            matrix=matrix,
            source_type=source,
            collection_type="composite" if composite_hours else "grab",
            collected_at=collected_at,
            collection_started_at=collected_at - timedelta(hours=composite_hours)
            if composite_hours
            else None,
            collector_name=collector,
            collection_location=location or f"Synthetic sampling point {ident}",
            received_at=received_at,
            cooler_temperature=Decimal(temperature),
            coolant="wet_ice",
            temperature_instrument="Synthetic IR thermometer DEMO-IR-01",
            seal_condition="Intact",
            preservation_check="Synthetic: checked at receipt for demonstration",
            discrepancy=discrepancy,
            disposition=disposition,
            disposition_note=note,
            current_location="Receipt bench",
        )
        return sample

    def store(self, sample):
        """Move a received sample from the receipt bench into cold storage."""
        name = self.custodian.get_full_name() or self.custodian.username
        sample.refresh_from_db(fields=["revision"])
        return receipt.transfer_sample(
            actor=self.custodian,
            sample_id=sample.pk,
            expected_revision=sample.revision,
            to_location="Walk-in cooler 1",
            relinquished_by=name,
            received_by=name,
            occurred_at=sample.received_at + timedelta(minutes=20),
            reason="Synthetic: stored after the receipt check",
        )

    # ------------------------------------------------------------------ analysis

    def qc(self, batch, definitions, at, *, run=None, labels=("MB-1", "LCS-1"), lcs=None):
        """Method blank and LCS for each test of a batch; ``run`` links preparation QC to its
        analytical run. ``lcs`` overrides observed LCS text by analyte name."""
        blank = bench.add_qc_sample(
            actor=self.analyst, batch_id=batch.pk, qc_type="method_blank", label=labels[0]
        )
        spiked = bench.add_qc_sample(
            actor=self.analyst, batch_id=batch.pk, qc_type="lcs", label=labels[1]
        )
        for d in definitions:
            common = dict(
                actor=self.analyst,
                definition_id=d.pk,
                reporting_limit_applied=_rl(d),
                analysis_started_at=at,
                analysis_batch_id=run.pk if run else None,
            )
            bench.record_qc_measurement(qc_sample_id=blank.pk, observed_text="ND", **common)
            bench.record_qc_measurement(
                qc_sample_id=spiked.pk,
                observed_text=(lcs or {}).get(d.analyte.name) or self.lcs(d),
                spike_value=SPIKE[d.matrix],
                **common,
            )

    def entry(self, rt, at, stage, **extra):
        """Take one requested test to ``stage``: awaiting, entered, submitted, returned or
        approved. The analyst enters and submits; the reviewer, a different person, decides."""
        if stage == "awaiting":
            return None
        text, qualifiers = extra.pop("value", None) or self.value(rt.definition)
        revision = bench.enter_result(
            actor=self.analyst,
            result_id=Result.objects.get(requested_test=rt).pk,
            supplied_text=text,
            reporting_limit_applied=_rl(rt.definition),
            dilution_factor="1",
            analysis_started_at=at,
            qualifier_codes=qualifiers,
            **extra,
        )
        if stage == "entered":
            return revision
        bench.submit_result(actor=self.analyst, revision_id=revision.pk)
        if stage == "returned":
            bench.return_result(
                actor=self.reviewer,
                revision_id=revision.pk,
                reason="Synthetic: confirm the dilution against the instrument run log.",
            )
        elif stage == "approved":
            bench.approve_result(
                actor=self.reviewer, revision_id=revision.pk, note="Synthetic demonstration review."
            )
        return revision

    def requested(self, samples, method):
        return list(
            RequestedTest.objects.filter(sample__in=samples, definition__method=method)
            .select_related("definition__analyte", "definition__method", "sample")
            .order_by("sample__received_at", "sample__lab_id", "definition__analyte__name")
        )

    def batch(
        self, samples, code, matrix, *, prepared_at, analysed_at, stage, stages=None, lcs=None
    ):
        """One aqueous analytical batch: tests, method blank and LCS, results to ``stage``
        (``stages`` overrides it by client sample ID). Finished batches are closed."""
        method = Method.objects.get(code=code, version=VERSION)
        batch = bench.create_batch(
            actor=self.analyst,
            method=method,
            matrix=matrix,
            prepared_at=prepared_at,
            equipment=self.icp,
            notes="Synthetic demonstration batch.",
        )
        tests = self.requested(samples, method)
        bench.add_tests_to_batch(
            actor=self.analyst, batch_id=batch.pk, requested_test_ids=[rt.pk for rt in tests]
        )
        definitions = list({rt.definition_id: rt.definition for rt in tests}.values())
        self.qc(batch, definitions, analysed_at, lcs=lcs)
        stages = stages or {}
        for rt in tests:
            self.entry(rt, analysed_at, stages.get(rt.sample.client_sample_id, stage))
        if not stages and stage != "awaiting":
            bench.close_batch(actor=self.analyst, batch_id=batch.pk)
        return batch

    def soil_batch(self, samples, *, prepared_at, completed_at, analysed_at):
        """Independent digestion, then analysis with blank, LCS and matrix spike; dry-basis
        results with their percent-solids source, approved by the reviewer."""
        method = Method.objects.get(code="DEMO-METALS-SOIL", version=VERSION)
        tests = self.requested(samples, method)
        definitions = list({rt.definition_id: rt.definition for rt in tests}.values())
        run = bench.create_batch(
            actor=self.analyst,
            method=method,
            matrix="soil",
            equipment=self.icp,
            notes="Synthetic demonstration batch.",
        )
        bench.add_tests_to_batch(
            actor=self.analyst, batch_id=run.pk, requested_test_ids=[rt.pk for rt in tests]
        )
        prep_method = (
            PreparationRequirement.objects.filter(definition=definitions[0], is_active=True)
            .select_related("method")
            .get()
            .method
        )
        prep = bench.create_batch(
            actor=self.analyst,
            kind="preparation",
            method=prep_method,
            matrix="soil",
            prepared_at=prepared_at,
            completed_at=completed_at,
            notes="Synthetic demonstration digestion.",
        )
        aliquots = {
            sample.pk: preparation.add_prepared_aliquot(
                actor=self.analyst,
                batch_id=prep.pk,
                sample_id=sample.pk,
                requested_test_ids=[rt.pk for rt in tests if rt.sample_id == sample.pk],
                label=f"DIG-{sample.client_sample_id}",
            )
            for sample in samples
        }
        values = {rt.pk: self.value(rt.definition) for rt in tests}
        self.qc(run, definitions, analysed_at)
        spike = bench.add_qc_sample(
            actor=self.analyst,
            batch_id=run.pk,
            qc_type="matrix_spike",
            label="MS-1",
            parent_sample=samples[0],
        )
        for rt in tests:
            if rt.sample_id != samples[0].pk:
                continue
            native = Decimal(values[rt.pk][0])
            bench.record_qc_measurement(
                actor=self.analyst,
                qc_sample_id=spike.pk,
                definition_id=rt.definition_id,
                observed_text=format(native + Decimal("48.0"), "f"),
                reporting_limit_applied=_rl(rt.definition),
                analysis_started_at=analysed_at,
                spike_value=SPIKE["soil"],
                native_value=format(native, "f"),
            )
        self.qc(prep, definitions, analysed_at, run=run, labels=("PMB-1", "PLCS-1"))
        solids = dict(zip((s.pk for s in samples), cycle(["81.6", "77.9", "84.2"]), strict=False))
        for rt in tests:
            self.entry(
                rt,
                analysed_at,
                "approved",
                value=values[rt.pk],
                prepared_aliquot_id=aliquots[rt.sample_id].pk,
                result_basis="dry",
                percent_solids=solids[rt.sample_id],
                solids_reference="Synthetic percent-solids worksheet DEMO-PS-01",
            )
        for batch in (prep, run):
            bench.close_batch(actor=self.analyst, batch_id=batch.pk)
        return run, prep

    # ------------------------------------------------------------------ reports

    def release(self, report, reason=""):
        report = Report.objects.get(pk=report.pk)
        fingerprint = assemble(report, actor=self.approver).fingerprint
        return release_report(
            actor=self.approver,
            report_id=report.pk,
            expected_fingerprint=fingerprint,
            reason=reason,
        )

    def report(self, project, title, samples):
        return create_report(
            actor=self.manager,
            project=project,
            title=title,
            samples=samples,
            narrative="Synthetic demonstration report. Values are invented.",
        )

    def correct(self, sample, analyte, text):
        """A reasoned correction of an approved result, reviewed by someone else."""
        old = (
            ResultRevision.objects.filter(
                result__requested_test__sample=sample,
                analyte_name=analyte,
                status=ResultRevision.Status.APPROVED,
            )
            .order_by("-number")
            .first()
        )
        if text == old.supplied_text:
            text = format(Decimal(text) + 2, "f")
        new = bench.enter_result(
            actor=self.analyst,
            result_id=old.result_id,
            supplied_text=text,
            reporting_limit_applied=format(old.reporting_limit_applied, "f"),
            dilution_factor="1",
            analysis_started_at=old.analysis_started_at,
            reason="Synthetic correction: transcription error found against the run log.",
        )
        bench.submit_result(actor=self.analyst, revision_id=new.pk)
        bench.approve_result(
            actor=self.reviewer, revision_id=new.pk, note="Synthetic re-review of the correction."
        )
        return new

    # ------------------------------------------------------------------ the week

    def history_days(self, count=16):
        """The ``count`` most recent weekdays between 8 and 40 days ago, oldest first."""
        days = [n for n in range(8, 41) if (self.today - timedelta(days=n)).weekday() < 5][:count]
        return sorted(days, reverse=True)

    def run(self):
        water = self.tests("DEMO-METALS-AQ", "water")
        wastewater = self.tests("DEMO-METALS-AQ", "wastewater")
        soil = self.tests("DEMO-METALS-SOIL", "soil")
        tss = self.tests("DEMO-TSS-AQ", "wastewater")
        bod = self.tests("DEMO-BOD-AQ", "wastewater")

        # Receiving water: one grab per weekday over the past weeks, each analysed the same day.
        # Their LCS recoveries are the control-chart history; the month is one released report.
        river = []
        for n, days in enumerate(self.history_days(), 1):
            order = self.order(self.river, self.at(days, 9, 30))
            sample = self.receive(
                order,
                f"RW2-{n:03d}",
                water,
                source="surface_water",
                received_at=self.at(days, 9, 30),
                collector="Example Collector",
                location="Synthetic Test Creek station RW-2",
            )
            self.store(sample)
            self.batch(
                [sample],
                "DEMO-METALS-AQ",
                "water",
                prepared_at=self.at(days, 11),
                analysed_at=self.at(days, 14),
                stage="approved",
            )
            river.append(sample)

        def outfall(days, idents, **batch):
            order = self.order(self.outfall, self.at(days, 10))
            samples = []
            for minute, ident in enumerate(idents):
                samples.append(
                    self.receive(
                        order,
                        ident,
                        water,
                        source="surface_water",
                        received_at=self.at(days, 10, minute * 5),
                        collector="Example Collector",
                        location="Synthetic Test Creek outfall 001",
                    )
                )
                self.store(samples[-1])
            self.batch(
                samples,
                "DEMO-METALS-AQ",
                "water",
                prepared_at=self.at(days, 12),
                analysed_at=self.at(days, 15),
                **batch,
            )
            return samples

        weekly = outfall(7, ["OF001-W01", "OF001-W02", "OF001-W03"], stage="approved")

        # Plant 2 wastewater, reviewed and waiting for its report.
        order = self.order(self.plant, self.at(5, 9))
        plant_done = [
            self.receive(
                order,
                ident,
                wastewater,
                source=source,
                received_at=self.at(5, 9, minute),
                composite_hours=24 if source == "influent" else None,
                location=f"Synthetic Plant 2 {source}",
            )
            for minute, (ident, source) in enumerate(
                [("P2-INF-101", "influent"), ("P2-EFF-101", "effluent"), ("P2-EFF-102", "effluent")]
            )
        ]
        for sample in plant_done:
            self.store(sample)
        self.batch(
            plant_done,
            "DEMO-METALS-AQ",
            "wastewater",
            prepared_at=self.at(5, 11),
            analysed_at=self.at(5, 14),
            stage="approved",
        )

        outfall(4, ["OF001-W04", "OF001-W05", "OF001-W06"], stage="approved")

        # Drying-bed soils: digested, analysed and reviewed on a dry basis.
        order = self.order(self.beds, self.at(3, 10))
        beds = [
            self.receive(
                order,
                f"DB-S{n:02d}",
                soil,
                source="sludge" if n == 3 else "soil",
                received_at=self.at(3, 10, n),
                location=f"Synthetic drying bed {n}",
            )
            for n in (1, 2, 3)
        ]
        for sample in beds:
            self.store(sample)
        self.soil_batch(
            beds,
            prepared_at=self.at(3, 13),
            completed_at=self.at(3, 15),
            analysed_at=self.at(2, 10),
        )

        # Outfall batch with a failed LCS for lead: submitted, needs a QC disposition.
        outfall(2, ["OF001-W07", "OF001-W08", "OF001-W09"], stage="submitted", lcs={"Lead": "26.0"})
        # Outfall batch with passing QC, submitted and awaiting technical review.
        outfall(1, ["OF001-W10", "OF001-W11", "OF001-W12", "OF001-W13"], stage="submitted")

        # Plant 2 samples from yesterday: in analysis today (entered, returned, awaiting entry).
        order = self.order(self.plant, self.at(1, 14), priority="rush")
        bench_today = [
            self.receive(
                order,
                ident,
                wastewater,
                source=source,
                received_at=self.at(1, 14, minute),
                composite_hours=24 if source == "influent" else None,
                location=f"Synthetic Plant 2 {source}",
            )
            for minute, (ident, source) in enumerate(
                [
                    ("P2-INF-102", "influent"),
                    ("P2-EFF-103", "effluent"),
                    ("P2-EFF-104", "effluent"),
                    ("P2-EFF-105", "effluent"),
                ]
            )
        ]
        for sample in bench_today:
            self.store(sample)
        self.batch(
            bench_today,
            "DEMO-METALS-AQ",
            "wastewater",
            prepared_at=self.at(1, 16),
            analysed_at=self.ago(hours=2),
            stage="awaiting",
            stages={"P2-INF-102": "returned", "P2-EFF-103": "entered", "P2-EFF-104": "entered"},
        )
        self.receive(
            order,
            "P2-EFF-106",
            wastewater,
            source="effluent",
            received_at=self.at(1, 14, 10),
            location="Synthetic Plant 2 effluent",
            discrepancy="Synthetic: container arrived cracked with less than half its volume.",
            disposition="reject",
            note="Synthetic: insufficient volume for the tests; client asked to resample.",
        )

        # Just received today: still on the receipt bench, not yet in a batch.
        order = self.order(self.plant, self.ago(hours=3))
        for minute, (ident, source, extra) in enumerate(
            [
                ("P2-INF-103", "influent", bod),
                ("P2-EFF-107", "effluent", bod),
                ("P2-EFF-108", "effluent", []),
                ("P2-EFF-109", "effluent", []),
            ]
        ):
            self.receive(
                order,
                ident,
                [*wastewater, *tss, *extra],
                source=source,
                received_at=self.ago(hours=3, minutes=-minute * 4),
                composite_hours=24 if source == "influent" else None,
                location=f"Synthetic Plant 2 {source}",
            )
        self.receive(
            order,
            "P2-EFF-110",
            [*wastewater, *tss],
            source="effluent",
            received_at=self.ago(hours=2, minutes=40),
            location="Synthetic Plant 2 effluent",
            discrepancy="Synthetic: bottle label reads P2-EFF-01, the chain of custody P2-EFF-110.",
            disposition="hold",
            note="Synthetic: on hold until the client confirms the sample identity.",
        )
        order = self.order(self.beds, self.ago(hours=1, minutes=30), delivery="client")
        for n in (4, 5):
            self.receive(
                order,
                f"DB-S{n:02d}",
                soil,
                source="soil",
                received_at=self.ago(hours=1, minutes=30 - n),
                location=f"Synthetic drying bed {n}",
            )

        self.mdl_study(water)

        # Reports: the receiving-water month (released), the outfall week (released, then
        # amended after a reviewed correction) and a draft for Plant 2.
        month = self.report(self.river, "Test Creek receiving water, monthly summary", river)
        self.release(month)
        week = self.report(self.outfall, "Outfall 001 weekly monitoring", weekly)
        self.release(week)
        self.correct(weekly[0], "Zinc", "46")
        self.release(
            week,
            reason="Synthetic amendment: corrected zinc result for OF001-W01 after a "
            "transcription check.",
        )
        self.report(self.plant, "Plant 2 metals, week summary", plant_done)

    def mdl_study(self, water):
        """An initial MDL study for lead in water: seven spikes over three preparation batches
        and the routine method blanks, calculated and waiting for an independent review."""
        lead = next(d for d in water if d.analyte.name == "Lead")
        study = mdl_services.create_study(
            actor=self.analyst,
            definition=lead,
            kind="initial",
            spiking_level="2.00",
            instrument_note="DEMO-ICP-01 (synthetic)",
            notes="Synthetic demonstration study. Values are invented.",
        )
        for n, (days, label, text) in enumerate(MDL_SPIKES, 1):
            mdl_services.add_observation(
                actor=self.analyst,
                study_id=study.pk,
                kind="spike",
                supplied_text=text,
                prepared_at=self.at(days, 9),
                analyzed_at=self.at(days, 13, n),
                batch_label=label,
                preparation_label=f"{label}-S{n}",
                spike_level="2.00",
                qualitative_ok=True,
            )
        mdl_services.import_blanks(
            actor=self.analyst,
            study_id=study.pk,
            start=self.today - timedelta(days=60),
            end=self.today,
        )
        mdl_services.calculate(actor=self.analyst, study_id=study.pk)
        return study
