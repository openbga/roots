"""The first journey of docs/product.md, outcome by outcome, on synthetic records.

Records are created through the domain services and forms the application itself uses; who
may do what, downloads and the client portal are exercised through HTTPS requests against
the real URL configuration. Every name, value and limit here is invented.
"""

import hashlib
import json
import re
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.utils import timezone

from accounts.models import User
from accounts.setup import setup_lab
from analytical import services as bench
from analytical.holding import duration_label, running_clocks
from analytical.models import HoldingTimeRule, QCLimit, QCMeasurement, Result, ResultRevision
from analytical.qc import BatchQC
from analytical.synthetic_dataset import seed_synthetic_dataset
from catalog.models import Method, TestDefinition
from core.models import AuditEvent, Lab
from receipt import services as receipt
from receipt.models import Client, ClientContact, CustodyEvent, Order, Project, Sample
from reporting.content import change_text
from reporting.models import Report, ReportFile, ReportVersion
from reporting.services import create_report

from .runner import form_errors, role_refusal, step

LAB_NAME = "Acceptance Example Laboratory (synthetic)"
VERSION = "demo-1"
METALS = "DEMO-METALS-AQ"
NUTRIENTS = "DEMO-NUTRIENTS-AQ"
OWN_RULE_ANALYTE = "Total phosphorus"  # has a holding rule of its own in the nutrients method
STAFF = {  # account suffix -> roles; "approver" carries release permission through its group
    "custodian": ["custodian"],
    "analyst": ["analyst"],
    "reviewer": ["reviewer"],
    "approver": ["approver"],
    "manager": ["project_manager"],
    "analyst_reviewer": ["analyst", "reviewer"],
}
COC_A = "ACC-COC-0001"


# ----------------------------------------------------------------------------- helpers


def now_minute():
    """The current time without seconds, so displayed and computed times compare exactly."""
    return timezone.now().replace(second=0, microsecond=0)


def sha256(data):
    return hashlib.sha256(bytes(data)).hexdigest()


def account(run, name):
    return User.objects.get(username=f"acceptance_{name}")


def signed_in(run, name):
    """A signed-in HTTPS session for an acceptance account, opened once per run."""
    sessions = run.records.setdefault("sessions", {})
    if name not in sessions:
        browser = run.browser()
        response = browser.sign_in(f"acceptance_{name}", run["passwords"][name])
        run.check(
            response.status_code == 302 and "/accounts/login" not in response.get("Location", ""),
            f"acceptance_{name} signed in through the sign-in form (HTTP {response.status_code})",
            "a redirect into the application",
        )
        sessions[name] = browser
    return sessions[name]


def tests_for(code, matrix="water"):
    return list(
        TestDefinition.objects.filter(method__code=code, method__version=VERSION, matrix=matrix)
        .select_related("analyte", "method")
        .order_by("analyte__name")
    )


def receive(run, order, ident, tests, *, collected_at, received_at, **extra):
    """Receive one sample on an order through the receipt service, as the custodian."""
    data = dict(
        client_sample_id=ident,
        matrix="water",
        source_type="surface_water",
        collection_type="grab",
        collected_at=collected_at,
        collection_started_at=None,
        collector_name="Example Field Collector (synthetic)",
        collection_location=f"Synthetic Test Creek station {ident}",
        received_at=received_at,
        cooler_temperature=Decimal("4.20"),
        coolant="wet_ice",
        temperature_instrument="Synthetic IR thermometer ACC-IR-01",
        seal_condition="Intact",
        preservation_check="Synthetic: pH below 2 confirmed by test strip",
        discrepancy="",
        disposition="proceed",
        disposition_note="",
        current_location="Receipt bench",
    )
    data.update(extra)
    return receipt.receive_order_sample(
        actor=account(run, "custodian"),
        order_id=order.pk,
        expected_revision=Order.objects.values_list("revision", flat=True).get(pk=order.pk),
        tests=tests,
        containers=[
            {
                "description": "Synthetic 500 mL HDPE",
                "condition": "Intact",
                "preservation": "HNO3 to pH < 2",
            }
        ],
        relinquished_by="Example Courier (synthetic)",
        **data,
    )


def build_batch(run, samples, *, lcs=None, prepared_minutes_ago=50):
    """A metals batch with the samples' tests, a method blank and an LCS measured per analyte."""
    analyst = account(run, "analyst")
    batch = bench.create_batch(
        actor=analyst,
        method=Method.objects.get(code=METALS, version=VERSION),
        matrix="water",
        prepared_at=timezone.now() - timedelta(minutes=prepared_minutes_ago),
        notes="Synthetic acceptance batch.",
    )
    tests = [rt for s in samples for rt in s.requested_tests.select_related("definition__analyte")]
    bench.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[rt.pk for rt in tests]
    )
    blank = bench.add_qc_sample(
        actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="MB-1"
    )
    spike = bench.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="lcs", label="LCS-1")
    analysed = timezone.now() - timedelta(minutes=30)
    for definition in {rt.definition_id: rt.definition for rt in tests}.values():
        common = dict(
            actor=analyst,
            definition_id=definition.pk,
            reporting_limit_applied=format(definition.reporting_limit, "f"),
            analysis_started_at=analysed,
        )
        bench.record_qc_measurement(qc_sample_id=blank.pk, observed_text="ND", **common)
        bench.record_qc_measurement(
            qc_sample_id=spike.pk,
            observed_text=(lcs or {}).get(definition.analyte.name, "19.8"),
            spike_value="20.0",
            **common,
        )
    return batch


def enter(run, sample, analyte, text, *, rl, dilution="1", qualifiers=(), reason="", who="analyst"):
    result = Result.objects.get(
        requested_test__sample=sample, requested_test__definition__analyte__name=analyte
    )
    return bench.enter_result(
        actor=account(run, who),
        result_id=result.pk,
        supplied_text=text,
        reporting_limit_applied=rl,
        dilution_factor=dilution,
        analysis_started_at=timezone.now() - timedelta(minutes=20),
        qualifier_codes=qualifiers,
        reason=reason,
    )


def fingerprint(response):
    text = response.content.decode("utf-8", "replace")
    for pattern in (
        r'name="fingerprint"[^>]*value="([0-9a-f]{64})"',
        r'value="([0-9a-f]{64})"[^>]*name="fingerprint"',
    ):
        if found := re.search(pattern, text):
            return found.group(1)
    return None


def release_over_http(run, report, *, reason=None, confirm=True, who="approver"):
    """Open the report preview and post its release form, as a person would."""
    browser = signed_in(run, who)
    preview = browser.get(f"/reports/{report.pk}/preview/")
    data = {"fingerprint": fingerprint(preview) or ""}
    if confirm:
        data["confirm"] = "on"
    if reason is not None:
        data["reason"] = reason
    return browser.post(f"/reports/{report.pk}/release/", data)


def download(run, who, path):
    response = signed_in(run, who).get(path)
    return response, (
        b"".join(response.streaming_content) if response.streaming else response.content
    )


# ------------------------------------------------------------------------------- setup


@step(
    "E01",
    "HTTPS, host and cookie settings as configured",
    requirement="The run's requests pass through the deployment's own security settings; the "
    "evidence records which settings were in effect.",
    expected="The sign-in, health and readiness pages answer over HTTPS. Where SECURE_SSL_REDIRECT "
    "is on, a plain HTTP request is redirected to HTTPS; where HSTS is configured, the header is "
    "sent; the CSRF cookie's Secure flag matches the setting.",
)
def environment(run):
    browser = run.browser()
    for path in ("/health/", "/ready/", "/accounts/login/"):
        response = browser.get(path)
        run.check(
            response.status_code == 200,
            f"GET {path} over HTTPS: HTTP {response.status_code}",
            "200; a 500 on the sign-in page usually means collectstatic has not been run",
        )
    run.observe(
        f"Settings in effect: DEBUG={settings.DEBUG}, "
        f"SECURE_SSL_REDIRECT={settings.SECURE_SSL_REDIRECT}, "
        f"SECURE_HSTS_SECONDS={settings.SECURE_HSTS_SECONDS}, SESSION_COOKIE_SECURE="
        f"{settings.SESSION_COOKIE_SECURE}, CSRF_COOKIE_SECURE={settings.CSRF_COOKIE_SECURE}"
    )
    plain = browser.get("/accounts/login/", secure=False)
    if settings.SECURE_SSL_REDIRECT:
        run.check(
            plain.status_code == 301 and plain["Location"].startswith("https://"),
            f"Plain HTTP request: HTTP {plain.status_code} to an https:// address",
            "301 to HTTPS",
        )
    else:
        run.observe(
            f"Plain HTTP request: HTTP {plain.status_code}; SECURE_SSL_REDIRECT is off in these "
            "settings, so no redirect is expected (development settings, not production)"
        )
    hsts = browser.get("/accounts/login/").get("Strict-Transport-Security")
    if settings.SECURE_HSTS_SECONDS:
        run.check(bool(hsts), f"Strict-Transport-Security header: '{hsts}'")
    else:
        run.observe("HSTS is not configured in these settings; no header expected")
    cookie = browser.client.cookies.get(settings.CSRF_COOKIE_NAME)
    run.check(
        cookie is not None and bool(cookie["secure"]) == settings.CSRF_COOKIE_SECURE,
        f"CSRF cookie Secure={bool(cookie['secure']) if cookie else None}",
        f"Secure={settings.CSRF_COOKIE_SECURE}",
    )


@step(
    "S01",
    "Set up the synthetic laboratory and its staff accounts",
    requirement="A fresh installation can be set up as documented: the laboratory, its role "
    "groups and an administrator through setup_lab, then staff accounts through the account "
    "administration form; the synthetic method set loads.",
    expected="The laboratory and six role groups exist; the administrator signs in over HTTPS "
    "and creates six staff accounts with their roles; only the approver group carries release "
    "permission; the synthetic DEMO- methods with holding rules and QC limits exist.",
)
def set_up_laboratory(run):
    passwords = {name: secrets.token_urlsafe(18) for name in ["admin", *STAFF]}
    run.secrets.extend(passwords.values())
    run["passwords"] = passwords
    result = setup_lab(
        lab_name=LAB_NAME,
        username="acceptance_admin",
        email="acceptance_admin@example.invalid",
        password=passwords["admin"],
        first_name="Acceptance",
        last_name="Administrator",
    )
    run.check(
        Lab.objects.filter(name=LAB_NAME).exists() and result.changed,
        f"setup_lab created laboratory '{LAB_NAME}' and administrator acceptance_admin",
    )
    groups = dict(Group.objects.values_list("name", "pk"))
    run.check(
        {"admin", "custodian", "analyst", "reviewer", "approver", "project_manager"} <= set(groups),
        f"Role groups present: {', '.join(sorted(groups))}",
    )
    releasing = sorted(
        Group.objects.filter(permissions__codename="authorize_release").values_list(
            "name", flat=True
        )
    )
    run.check(
        releasing == ["approver"],
        f"Groups carrying release permission: {releasing}",
        "['approver']",
    )

    admin = signed_in(run, "admin")
    session = admin.client.cookies.get(settings.SESSION_COOKIE_NAME)
    run.check(
        session is not None
        and bool(session["httponly"])
        and bool(session["secure"]) == settings.SESSION_COOKIE_SECURE,
        f"Session cookie HttpOnly={bool(session['httponly']) if session else None}, "
        f"Secure={bool(session['secure']) if session else None} "
        f"(SESSION_COOKIE_SECURE={settings.SESSION_COOKIE_SECURE})",
    )
    for name, roles in STAFF.items():
        response = admin.post(
            "/admin/accounts/user/add/",
            {
                "username": f"acceptance_{name}",
                "email": f"acceptance_{name}@example.invalid",
                "usable_password": "true",
                "password1": passwords[name],
                "password2": passwords[name],
                "client": "",
                "groups": [groups[r] for r in roles],
                "_save": "Save",
            },
        )
        created = User.objects.filter(username=f"acceptance_{name}").first()
        run.check(
            response.status_code == 302
            and created is not None
            and sorted(created.groups.values_list("name", flat=True)) == sorted(roles),
            f"Account acceptance_{name} created in account administration with roles "
            f"{', '.join(roles)} (HTTP {response.status_code})",
            "HTTP 302 and the account with exactly these roles"
            + (f"; form said: {form_errors(response)}" if response.status_code == 200 else ""),
        )
    created = AuditEvent.objects.filter(
        action="account.created", actor__username="acceptance_admin"
    )
    run.check(
        created.count() == len(STAFF),
        f"{created.count()} account.created audit events attributed to acceptance_admin",
    )
    counts = seed_synthetic_dataset()
    run.check(
        Method.objects.filter(code__startswith="DEMO-").count() >= 6
        and HoldingTimeRule.objects.exists()
        and QCLimit.objects.exists(),
        "Synthetic method set loaded: "
        + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items()),
    )


@step(
    "S02",
    "A second client with its own received sample",
    requirement="Background for the client-isolation checks: a second client, project and "
    "received sample owned by that client.",
    expected="Client 'Sample Water District (synthetic)' with one project and one received "
    "water sample for metals.",
    depends=("S01",),
)
def second_client(run):
    manager = account(run, "manager")
    client = receipt.create_client(
        actor=manager,
        name="Sample Water District (synthetic)",
        account_number="ACC-SWD",
        contact_name="Synthetic Operator",
        email="operator@example.invalid",
    )
    project = receipt.create_project(
        actor=manager, client=client, name="Example Reservoir (synthetic)", number="ACC-0002"
    )
    order = receipt.create_order(
        actor=account(run, "custodian"), project=project, coc_reference="ACC-COC-0002"
    )
    now = now_minute()
    sample = receive(
        run,
        order,
        "RES-001",
        tests_for(METALS),
        collected_at=now - timedelta(hours=3),
        received_at=now - timedelta(hours=1),
    )
    run["client_b"], run["project_b"], run["sample_b1"] = client, project, sample
    run.check(True, f"Client {client.name}, project {project.name}, sample {sample.lab_id}")


# --------------------------------------------------------------------- the 13 outcomes


@step(
    "O01",
    "Client, contact and project",
    outcome=1,
    requirement="A project manager creates a client, contact and project with a permit or "
    "program label, required turnaround, and report preferences.",
    expected="The three forms accept the entries over HTTPS and the records hold exactly what "
    "was entered.",
    depends=("S01",),
)
def client_and_project(run):
    browser = signed_in(run, "manager")
    response = browser.post(
        "/clients/new/",
        {
            "name": "Example Acceptance Utilities (synthetic)",
            "account_number": "ACC-EXU",
            "contact_name": "Synthetic Contact",
            "email": "contact@example.invalid",
            "phone": "555-0100",
            "address_line1": "100 Example Street",
            "city": "Exampleville",
            "state": "NY",
            "postal_code": "00000",
            "payment_terms": "",
            "report_preferences": "PDF report to the primary contact. Synthetic.",
        },
    )
    client = Client.objects.filter(name="Example Acceptance Utilities (synthetic)").first()
    run.check(
        response.status_code == 302 and client is not None,
        f"New client form: HTTP {response.status_code}",
        "a redirect to the new client"
        + (f"; form said: {form_errors(response)}" if response.status_code == 200 else ""),
    )
    run.check(
        client.report_preferences == "PDF report to the primary contact. Synthetic.",
        f"Client stored with report preferences '{client.report_preferences}'",
    )
    response = browser.post(
        f"/clients/{client.pk}/contacts/new/",
        {
            "name": "Synthetic Report Recipient",
            "email": "reports@example.invalid",
            "phone": "555-0101",
            "receives_reports": "on",
        },
    )
    contact = ClientContact.objects.filter(client=client).first()
    run.check(
        response.status_code == 302 and contact is not None and contact.receives_reports,
        f"New contact form: HTTP {response.status_code}; contact "
        f"'{contact.name if contact else None}' receives reports",
        "a redirect and the stored contact"
        + (f"; form said: {form_errors(response)}" if response.status_code == 200 else ""),
    )
    manager = account(run, "manager")
    response = browser.post(
        "/projects/new/",
        {
            "client": client.pk,
            "name": "Test Creek Outfall 001 (acceptance)",
            "number": "ACC-0001",
            "project_manager": manager.pk,
            "turnaround_days": "7",
            "regulatory_program": "npdes",
            "program": "SYNTH-PERMIT-0001",
            "site_state": "NY",
            "site_address": "Synthetic Test Creek treatment plant",
            "po_number": "PO-ACC-0001",
            "instructions": "Synthetic standing instruction.",
        },
    )
    project = Project.objects.filter(
        client=client, name="Test Creek Outfall 001 (acceptance)"
    ).first()
    run.check(
        response.status_code == 302 and project is not None,
        f"New project form: HTTP {response.status_code}",
        "a redirect and the stored project"
        + (f"; form said: {form_errors(response)}" if response.status_code == 200 else ""),
    )
    run.check(
        (
            project.program,
            project.regulatory_program,
            project.turnaround_days,
            project.project_manager_id,
        )
        == ("SYNTH-PERMIT-0001", "npdes", 7, manager.pk),
        f"Project stored with permit label {project.program}, "
        f"program {project.regulatory_program}, "
        f"turnaround {project.turnaround_days} days, project manager {manager.username}",
    )
    run["client_a"], run["project_a"] = client, project


@step(
    "O02",
    "Chain of custody entered as an order",
    outcome=2,
    requirement="A custodian enters a COC as an order: sample IDs, collection date and time, "
    "matrix, requested tests, collector name.",
    expected="The order exists for the project; the received sample holds the client sample ID, "
    "collection time with its offset, matrix, the three requested tests and the collector exactly "
    "as entered.",
    depends=("O01",),
)
def chain_of_custody(run):
    now = now_minute()
    order = receipt.create_order(
        actor=account(run, "custodian"),
        project=run["project_a"],
        coc_reference=COC_A,
        due_at=now + timedelta(days=7),
        priority="standard",
        cooler_count=1,
        instructions="Synthetic shipment for the acceptance run.",
    )
    run.check(
        order.project_id == run["project_a"].pk and order.po_number == "PO-ACC-0001",
        f"Order {order.coc_reference} created for the project; it took the project's standing "
        f"PO {order.po_number}",
    )
    collected = now - timedelta(hours=3)
    tests = tests_for(METALS)
    sample = receive(
        run, order, "OF001-A1", tests, collected_at=collected, received_at=now - timedelta(hours=1)
    )
    sample = Sample.objects.get(pk=sample.pk)
    requested = sorted(sample.requested_tests.values_list("definition__analyte__name", flat=True))
    run.check(
        (sample.client_sample_id, sample.matrix, sample.collector_name, sample.collected_at)
        == ("OF001-A1", "water", "Example Field Collector (synthetic)", collected),
        f"Sample {sample.lab_id}: client ID {sample.client_sample_id}, matrix {sample.matrix}, "
        f"collected {timezone.localtime(sample.collected_at).isoformat()}, "
        f"collector {sample.collector_name}",
    )
    run.check(
        requested == sorted(t.analyte.name for t in tests),
        f"Requested tests: {', '.join(requested)} ({METALS} {VERSION})",
    )
    run["order_a"], run["sample_a1"] = order, sample


@step(
    "O03",
    "Receipt, inspection and discrepancy disposition",
    outcome=3,
    requirement="At receipt the custodian records cooler temperature, seal condition, container "
    "condition, field preservation checks and any discrepancy. Each discrepancy needs a "
    "disposition (proceed, qualify, hold, reject) and a note. Lab sample IDs are assigned.",
    expected="A discrepancy without a note is refused and nothing is saved; with a note it is "
    "stored with its disposition; the temperature keeps its original reading; every sample gets "
    "a distinct laboratory ID and a receipt custody event.",
    depends=("O02",),
)
def receipt_inspection(run):
    now = now_minute()
    order = run["order_a"]
    before = Sample.objects.count()
    try:
        receive(
            run,
            order,
            "OF001-A2",
            tests_for(METALS),
            collected_at=now - timedelta(hours=3),
            received_at=now - timedelta(hours=1),
            discrepancy="Synthetic: label smudged",
            disposition="qualify",
        )
        refused = False
    except ValidationError as exc:
        refused, message = True, " ".join(exc.messages)
    run.check(
        refused and "disposition note" in message and Sample.objects.count() == before,
        f"Discrepancy without a note refused: '{message if refused else 'accepted'}'; "
        "no sample saved",
    )
    a2 = receive(
        run,
        order,
        "OF001-A2",
        tests_for(METALS),
        collected_at=now - timedelta(hours=3),
        received_at=now - timedelta(hours=1),
        discrepancy="Synthetic: container label smudged; identity confirmed against the COC.",
        disposition="qualify",
        disposition_note="Synthetic: proceed and note the label condition on the report.",
    )
    a2 = Sample.objects.get(pk=a2.pk)
    run.check(
        (a2.disposition, a2.cooler_temperature, a2.cooler_temperature_original, a2.seal_condition)
        == ("qualify", Decimal("4.20"), "4.20", "Intact"),
        f"{a2.lab_id}: disposition {a2.disposition} with note; cooler {a2.cooler_temperature} "
        f"(original reading '{a2.cooler_temperature_original}'), seal {a2.seal_condition}, "
        f"preservation check '{a2.preservation_check}'",
    )
    container = a2.containers.get()
    run.check(
        container.condition == "Intact" and container.label.startswith(a2.lab_id),
        f"Container {container.label}: {container.description}, condition {container.condition}, "
        f"preservation {container.preservation}",
    )
    nutrients = tests_for(NUTRIENTS)
    a3 = receive(  # 44 hours into a 48-hour demonstration hold: at risk
        run,
        order,
        "OF001-A3",
        nutrients,
        collected_at=now - timedelta(hours=44),
        received_at=now - timedelta(hours=42),
    )
    a4 = receive(  # 72 hours into a 48-hour demonstration hold: expired
        run,
        order,
        "OF001-A4",
        nutrients,
        collected_at=now - timedelta(hours=72),
        received_at=now - timedelta(hours=70),
    )
    samples = [run["sample_a1"], a2, a3, a4]
    ids = [s.lab_id for s in samples]
    run.check(
        len(set(ids)) == 4 and all(s.lab_id and s.lab_id != s.client_sample_id for s in samples),
        f"Laboratory IDs assigned by the system: {', '.join(ids)}",
    )
    receipts = CustodyEvent.objects.filter(sample__in=samples, kind="receipt").count()
    run.check(receipts == 4, f"{receipts} receipt custody events recorded", "4")
    run["sample_a2"], run["sample_a3"], run["sample_a4"] = a2, a3, a4


CLOCK_FLAG = re.compile(
    r'data-vr-clock="(at_risk|expired)">\s*<span[^>]*data-tone="(warning|danger)"[^>]*>.*?</svg>'
    r"\s*(At risk|Expired) ·",
    re.S,
)
CLOCK_FLAGS = {("at_risk", "warning", "At risk"), ("expired", "danger", "Expired")}


def shown_clock_flags(html):
    """The holding-clock flags a page shows: the clock pill's stable hook (data-vr-clock) whose
    visible status pill has the matching tone and opens with the matching label."""
    return {
        label
        for hook, tone, label in CLOCK_FLAG.findall(html)
        if (hook, tone, label) in CLOCK_FLAGS
    }


@step(
    "O04",
    "Holding-time clocks computed from the rules",
    outcome=4,
    requirement="Each sample shows its holding-time clocks and flags samples at risk or "
    "expired. Expiry is computed from the rules, never typed.",
    expected="Each sample page shows, for every requested test, the deadline equal to collection "
    "time plus the duration of the rule that applies to it: the method's rule, or the analyte's "
    "own rule where one is configured. A sample 44 hours into a 48-hour rule is flagged At risk, "
    "one 72 hours in is flagged Expired, a sample well within its rule carries no flag.",
    depends=("O03",),
)
def holding_clocks(run):
    browser = signed_in(run, "custodian")
    now = timezone.now()
    for key, label in (("sample_a1", None), ("sample_a3", "At risk"), ("sample_a4", "Expired")):
        sample = Sample.objects.get(pk=run[key].pk)
        page = browser.get(f"/samples/{sample.pk}/")
        html = page.content.decode("utf-8", "replace")
        flagged = shown_clock_flags(html)
        statuses = set()
        tests = sample.requested_tests.select_related("definition__method", "definition__analyte")
        for definition in sorted((t.definition for t in tests), key=lambda d: d.analyte.name):
            # Worst first; these methods have one collection-based clock per analyte.
            clocks = running_clocks(
                definition.method,
                "water",
                definition.analyte_id,
                collected_at=sample.collected_at,
                now=now,
            )
            if not clocks:
                run.check(
                    False,
                    f"{sample.lab_id} {definition.analyte.name}: no holding-time rule applies",
                    "a holding-time rule for every requested test",
                )
                continue
            clock = clocks[0]
            rule = HoldingTimeRule.objects.get(pk=clock.rule_id)
            statuses.add(clock.status)
            deadline = timezone.localtime(sample.collected_at + rule.duration)
            run.check(
                page.status_code == 200 and f'datetime="{deadline.isoformat()}"' in html,
                f"{sample.lab_id} {definition.analyte.name}: page shows deadline "
                f"{deadline.isoformat()} = collection "
                f"{timezone.localtime(sample.collected_at).isoformat()} + rule '{rule.name}' "
                f"({duration_label(rule.duration)}, applies to {rule.applies_to.lower()})",
                "the computed deadline on the sample page",
            )
        run.check(
            (label is None and not flagged) or (label in flagged),
            f"{sample.lab_id}: flag shown {sorted(flagged) or 'none'}; clock statuses "
            f"{sorted(statuses) or 'none'}",
            f"flag {label or 'none'}",
        )


@step(
    "O05",
    "Analytical batch with required QC",
    outcome=5,
    requirement="An analyst builds a batch for one method, adds samples and required QC "
    "samples, and enters or imports results.",
    expected="Two batches of the aqueous metals method: one with samples from both clients, one "
    "with the qualified sample; each holds a method blank and an LCS measured for every analyte, "
    "so no required QC is missing.",
    depends=("O03", "S02"),
)
def batches(run):
    first = build_batch(run, [run["sample_a1"], run["sample_b1"]])
    second = build_batch(run, [run["sample_a2"]], lcs={"Lead": "26.0"})
    for batch in (first, second):
        # A finding without a measurement is required QC that is missing or short.
        issues = [i for i in BatchQC.load(batch).all_issues() if i.blocking]
        missing = [i.message for i in issues if not i.measurement_id]
        labels = ", ".join(batch.qc_samples.values_list("label", flat=True))
        run.check(
            not missing,
            f"Batch {batch.number} ({batch.method}, water): "
            f"{Result.objects.filter(batch=batch).count()} tests; QC samples {labels} measured "
            f"for every analyte; required QC missing: {missing or 'none'}",
            "none missing",
        )
    run["batch_1"], run["batch_2"] = first, second


@step(
    "O06",
    "Results with their full context; non-detects stay non-detects",
    outcome=6,
    requirement="Results carry value, unit, qualifier, reporting limit used, dilution, analysis "
    "date and time, analyst and method version. Non-detects are stored as non-detects, not as "
    "zero.",
    expected="Each entered result keeps the typed text and exact decimal value, unit, qualifier, "
    "applied reporting limit, dilution, analysis time, analyst and method version; ND is stored "
    "as a non-detect with no numeric value.",
    depends=("O05",),
)
def results(run):
    a1, a2, b1 = run["sample_a1"], run["sample_a2"], run["sample_b1"]
    lead = enter(run, a1, "Lead", "0.7", rl="1.0", qualifiers=("J",))
    copper = enter(run, a1, "Copper", "ND", rl="2.0")
    zinc = enter(run, a1, "Zinc", "48", rl="10.0", dilution="2")
    others = [
        enter(run, b1, "Lead", "3.1", rl="1.0"),
        enter(run, b1, "Copper", "8.6", rl="2.0"),
        enter(run, b1, "Zinc", "36", rl="10.0"),
        enter(run, a2, "Lead", "5.6", rl="1.0"),
        enter(run, a2, "Copper", "21.7", rl="2.0"),
        enter(run, a2, "Zinc", "55", rl="10.0"),
    ]
    lead = ResultRevision.objects.get(pk=lead.pk)
    run.check(
        (
            lead.supplied_text,
            lead.value,
            lead.unit,
            lead.reporting_limit_applied,
            lead.dilution_factor,
        )
        == ("0.7", Decimal("0.7"), "ug/L", Decimal("1.0"), Decimal("1"))
        and [q["code"] for q in lead.qualifiers] == ["J"],
        f"Lead: text '{lead.supplied_text}', value {lead.value} {lead.unit}, qualifier "
        f"{[q['code'] for q in lead.qualifiers]}, reporting limit {lead.reporting_limit_applied}, "
        f"dilution {lead.dilution_factor}",
    )
    run.check(
        lead.entered_by.username == "acceptance_analyst"
        and (lead.method_code, lead.method_version) == (METALS, VERSION)
        and lead.analysis_started_at is not None,
        f"Lead: analysed {timezone.localtime(lead.analysis_started_at).isoformat()} by "
        f"{lead.entered_by.username}, method {lead.method_code} version {lead.method_version}",
    )
    copper = ResultRevision.objects.get(pk=copper.pk)
    run.check(
        copper.kind == "non_detect" and copper.value is None and copper.supplied_text == "ND",
        f"Copper: kind {copper.kind}, value {copper.value}, reporting limit "
        f"{copper.reporting_limit_applied} {copper.unit}",
        "a non-detect with no numeric value",
    )
    zinc = ResultRevision.objects.get(pk=zinc.pk)
    run.check(
        zinc.dilution_factor == Decimal("2") and zinc.value == Decimal("48"),
        f"Zinc: value {zinc.value} {zinc.unit}, dilution {zinc.dilution_factor}",
    )
    for revision in [lead, copper, zinc, *others]:
        bench.submit_result(actor=account(run, "analyst"), revision_id=revision.pk)
    submitted = ResultRevision.objects.filter(status="submitted").count()
    run.check(submitted == 9, f"{submitted} results submitted for review by the analyst", "9")


@step(
    "O07",
    "Failing QC blocks approval until a disposition is recorded",
    outcome=7,
    requirement="QC samples are evaluated against configured limits. A batch with failing QC "
    "cannot be approved without a recorded corrective action or disposition.",
    expected="The LCS recovery of 130 % for lead (limit 85-115 %) is a blocking finding; approval "
    "of the lead result is refused; after the reviewer records an accept-with-qualification "
    "disposition with a note, approval succeeds.",
    depends=("O06",),
)
def failing_qc(run):
    batch = run["batch_2"]
    reviewer = account(run, "reviewer")
    findings = [i for i in BatchQC.load(batch).all_issues() if i.blocking]
    failing = QCMeasurement.objects.filter(pk__in=[i.measurement_id for i in findings])
    run.check(
        len(findings) == 1
        and [(m.qc_sample.qc_type, m.analyte_name) for m in failing] == [("lcs", "Lead")],
        "Blocking QC findings: " + "; ".join(i.message for i in findings),
        "exactly one, the lead LCS",
    )
    (issue,) = findings
    lead = ResultRevision.objects.get(batch=batch, analyte_name="Lead", status="submitted")
    try:
        bench.approve_result(actor=reviewer, revision_id=lead.pk)
        refusal = None
    except ValidationError as exc:
        refusal = " ".join(exc.messages)
    run.check(
        refusal is not None
        and "No corrective action recorded" in refusal
        and ResultRevision.objects.get(pk=lead.pk).status == "submitted",
        f"Approval of lead refused: '{refusal}'",
        "the QC refusal naming the missing corrective action",
    )
    bench.record_corrective_action(
        actor=reviewer,
        batch_id=batch.pk,
        target_key=issue.key,
        disposition="accept_qualified",
        note="Synthetic: high LCS bias; sample result is far below any decision level.",
    )
    for revision in ResultRevision.objects.filter(batch=batch, status="submitted"):
        bench.approve_result(actor=reviewer, revision_id=revision.pk)
    approved = ResultRevision.objects.filter(batch=batch, status="approved").count()
    run.check(
        approved == 3,
        f"After the recorded disposition, {approved} results of batch {batch.number} approved",
        "3",
    )


@step(
    "O08",
    "Technical review by another person, with a returned result",
    outcome=8,
    requirement="A qualified person other than the analyst performs technical review. Reviewers "
    "can return results with a reason, which creates a visible re-analysis or correction trail.",
    expected="The reviewer approves five results and returns one with a reason through the review "
    "page; the analyst enters a new revision with a reason; the result page shows both revisions "
    "and the return reason.",
    depends=("O06",),
)
def technical_review(run):
    batch = run["batch_1"]
    browser = signed_in(run, "reviewer")
    revisions = {
        (r.result.requested_test.sample_id, r.analyte_name): r
        for r in ResultRevision.objects.filter(batch=batch, status="submitted").select_related(
            "result__requested_test"
        )
    }
    b1 = run["sample_b1"].pk
    returned = revisions.pop((b1, "Zinc"))
    page = f"/analytical/review/batches/{batch.pk}/"
    run.check(browser.get(page).status_code == 200, "Review page opened by the reviewer")
    decisions = [
        browser.post(
            page,
            {"action": "approve", "note": "", "revisions": [r.pk for r in revisions.values()]},
        ),
    ]
    reason = "Synthetic: confirm the zinc dilution against the instrument run log."
    decisions.append(
        browser.post(page, {"action": "return", "note": reason, "revisions": [returned.pk]})
    )
    run.check(
        [r.status_code for r in decisions] == [302, 302],
        f"Approve and return posted on the review page: HTTP {[r.status_code for r in decisions]}",
        "302 twice",
    )
    approved = ResultRevision.objects.filter(
        pk__in=[r.pk for r in revisions.values()], status="approved"
    )
    run.check(
        approved.count() == 5 and all(r.reviewed_by_id != r.entered_by_id for r in approved),
        f"{approved.count()} results approved by acceptance_reviewer; none entered by the reviewer",
        "5",
    )
    returned.refresh_from_db()
    run.check(
        returned.status == "returned" and returned.review_note == reason,
        f"Zinc of {returned.result.requested_test.sample.lab_id} returned with reason "
        f"'{returned.review_note}'",
    )
    second = enter(
        run,
        run["sample_b1"],
        "Zinc",
        "38",
        rl="10.0",
        reason="Synthetic: dilution checked; value re-read from the run log.",
    )
    bench.submit_result(actor=account(run, "analyst"), revision_id=second.pk)
    bench.approve_result(actor=account(run, "reviewer"), revision_id=second.pk)
    history = browser.get(f"/analytical/results/{returned.result_id}/")
    html = history.content.decode("utf-8", "replace")
    run.check(
        history.status_code == 200
        and "confirm the zinc dilution" in html
        and "re-read from the run log" in html,
        "Result page shows revision 1 (returned, with its reason) and revision 2 (with the "
        "correction reason, approved)",
    )


@step(
    "O09",
    "Explicit, recorded release authorization",
    outcome=9,
    requirement="Release is an explicit, recorded authorization by a user holding that "
    "permission: who, when and what was authorized. The reviewer may hold it.",
    expected="The project manager and the reviewer, who lack release permission, are refused "
    "(HTTP 403); the approver's release without the confirmation box is refused; with it, "
    "version 1 is stored with who, when and the content hash, and a report.released audit event.",
    depends=("O07", "O08"),
)
def release(run):
    report = create_report(
        actor=account(run, "manager"),
        project=run["project_a"],
        title="Outfall 001 acceptance report (synthetic)",
        samples=[run["sample_a1"], run["sample_a2"]],
        narrative="Synthetic acceptance report. Values are invented.",
    )
    run["report_a"] = report
    for who in ("manager", "reviewer"):
        response = signed_in(run, who).post(
            f"/reports/{report.pk}/release/", {"fingerprint": "0" * 64, "confirm": "on"}
        )
        run.check(
            role_refusal(response),
            f"Release by acceptance_{who}: HTTP {response.status_code}",
            "authorization 403 (not a CSRF failure)",
        )
    response = release_over_http(run, report, confirm=False)
    run.check(
        response.status_code == 422 and not ReportVersion.objects.filter(report=report).exists(),
        f"Release without the confirmation: HTTP {response.status_code}, nothing stored",
        "422",
    )
    response = release_over_http(run, report)
    version = ReportVersion.objects.filter(report=report, number=1).first()
    run.check(
        response.status_code == 302 and version is not None,
        f"Release by acceptance_approver: HTTP {response.status_code}",
        "302 and version 1"
        + (f"; page said: {form_errors(response)}" if response.status_code != 302 else ""),
    )
    event = AuditEvent.objects.get(action="report.released", object_id=str(version.pk))
    run.check(
        version.released_by.username == "acceptance_approver"
        and event.actor_id == version.released_by_id
        and event.after.get("content_sha256") == version.content_sha256,
        f"{report.number} v1 released by {version.released_by.username} at "
        f"{timezone.localtime(version.released_at).isoformat()}; content SHA-256 "
        f"{version.content_sha256[:16]}…; audit event report.released by {event.actor.username}",
    )


@step(
    "O10",
    "Final report stored as an immutable PDF with ID and version",
    outcome=10,
    requirement="The system produces a final report as a stored, immutable PDF with a unique ID "
    "and version.",
    expected="Downloading version 1 returns a PDF attachment whose bytes equal the stored file "
    "and whose SHA-256 equals the recorded hash; the report number is unique.",
    depends=("O09",),
)
def stored_pdf(run):
    report = Report.objects.get(pk=run["report_a"].pk)
    version = ReportVersion.objects.get(report=report, number=1)
    stored = ReportFile.objects.get(version=version)
    response, body = download(run, "manager", f"/reports/{report.pk}/v1.pdf")
    run.check(
        response.status_code == 200
        and response["Content-Type"] == "application/pdf"
        and "attachment" in response["Content-Disposition"]
        and body.startswith(b"%PDF-"),
        f"GET /reports/{report.pk}/v1.pdf: HTTP {response.status_code}, "
        f"{response['Content-Type']}, "
        f"{len(body)} bytes",
    )
    run.check(
        body == bytes(stored.pdf) and sha256(body) == version.pdf_sha256,
        f"Downloaded bytes equal the stored PDF; SHA-256 {version.pdf_sha256}",
    )
    snapshot = stored.snapshot
    run.check(
        version.filename in response["Content-Disposition"]
        and snapshot["content"]["report"]["number"] == report.number
        and snapshot["release"]["version"] == 1,
        f"Report ID {report.number} and version 1 recorded in the stored snapshot and the "
        f"download name {version.filename}",
    )
    run["v1_pdf"] = body
    run["v1_snapshot"] = sha256(json.dumps(stored.snapshot, sort_keys=True).encode())
    run.artifact(f"{report.number}-v1.pdf", body)


@step(
    "O11",
    "Amendment as a new version; the original stays retrievable",
    outcome=11,
    requirement="A change after issue produces a new report version that references the "
    "original, states the reason and shows what changed. The original stays retrievable.",
    expected="After a reviewed correction, release without a reason is refused; version 2 names "
    "version 1, carries the reason and lists the zinc change 48 to 46 ug/L; version 1 downloads "
    "byte for byte as before.",
    depends=("O10",),
)
def amendment(run):
    report = run["report_a"]
    correction = enter(
        run,
        run["sample_a1"],
        "Zinc",
        "46",
        rl="10.0",
        dilution="2",
        reason="Synthetic correction: transcription error found against the run log.",
    )
    bench.submit_result(actor=account(run, "analyst"), revision_id=correction.pk)
    bench.approve_result(actor=account(run, "reviewer"), revision_id=correction.pk)
    response = release_over_http(run, report, reason="")
    run.check(
        response.status_code == 422
        and not ReportVersion.objects.filter(report=report, number=2).exists(),
        f"Amendment without a reason: HTTP {response.status_code}, nothing stored",
        "422",
    )
    reason = "Synthetic amendment: corrected zinc result after a transcription check."
    response = release_over_http(run, report, reason=reason)
    v2 = ReportVersion.objects.filter(report=report, number=2).first()
    run.check(
        response.status_code == 302 and v2 is not None,
        f"Amendment release: HTTP {response.status_code}",
        "302 and version 2"
        + (f"; page said: {form_errors(response)}" if response.status_code != 302 else ""),
    )
    v1 = ReportVersion.objects.get(report=report, number=1)
    changes = " | ".join(change_text(c) for c in v2.changes)
    run.check(
        v2.amends_id == v1.pk
        and v2.amendment_reason == reason
        and "48" in changes
        and "46" in changes,
        f"v2 amends v{v2.amends.number}; reason '{v2.amendment_reason}'; changes: {changes}",
    )
    run.check(
        ReportFile.objects.get(version=v2).snapshot["release"]["amends"]["version"] == 1,
        "The v2 snapshot names version 1 as the version it amends",
    )
    again, body = download(run, "manager", f"/reports/{report.pk}/v1.pdf")
    run.check(
        again.status_code == 200 and body == run["v1_pdf"],
        f"v1 downloaded again: identical bytes, SHA-256 {sha256(body)}",
    )
    response, v2_body = download(run, "manager", f"/reports/{report.pk}/v2.pdf")
    run.check(
        response.status_code == 200 and sha256(v2_body) == v2.pdf_sha256 and v2_body != body,
        f"v2 downloaded: {len(v2_body)} bytes, SHA-256 {v2.pdf_sha256}",
    )
    run["v2_pdf"] = v2_body
    run.artifact(f"{report.number}-v2.pdf", v2_body)


@step(
    "O12",
    "Client user sees only their own work",
    outcome=12,
    requirement="A client user signs in and sees only their own projects, sample status and "
    "issued reports.",
    expected="The client account, created in account administration, signs in; the portal lists "
    "its own project, samples and report but not the other client's; both issued versions "
    "download with the stored bytes.",
    depends=("O11",),
)
def client_portal(run):
    create_client_user(run, "client_a", run["client_a"])
    browser = signed_in(run, "client_a")
    report = run["report_a"]
    pages = {
        path: browser.get(path)
        for path in ("/portal/projects/", "/portal/samples/", "/portal/reports/")
    }
    text = {p: r.content.decode("utf-8", "replace") for p, r in pages.items()}
    run.check(
        all(r.status_code == 200 for r in pages.values()),
        "Portal projects, samples and reports pages: HTTP 200",
    )
    run.check(
        run["project_a"].name in text["/portal/projects/"]
        and run["project_b"].name not in text["/portal/projects/"],
        f"Projects list shows '{run['project_a'].name}', not '{run['project_b'].name}'",
    )
    run.check(
        run["sample_a1"].lab_id in text["/portal/samples/"]
        and run["sample_b1"].lab_id not in text["/portal/samples/"],
        f"Samples list shows {run['sample_a1'].lab_id}, "
        f"not the other client's {run['sample_b1'].lab_id}",
    )
    run.check(report.number in text["/portal/reports/"], f"Reports list shows {report.number}")
    for number, expected in ((1, run["v1_pdf"]), (2, run["v2_pdf"])):
        response, body = download(run, "client_a", f"/portal/reports/{report.pk}/v{number}.pdf")
        run.check(
            response.status_code == 200 and body == expected,
            f"Portal download of v{number}: HTTP {response.status_code}, "
            "bytes equal the stored file",
        )


def create_client_user(run, name, client):
    """A client portal account, created by the administrator in account administration."""
    password = secrets.token_urlsafe(18)
    run.secrets.append(password)
    run["passwords"][name] = password
    response = signed_in(run, "admin").post(
        "/admin/accounts/user/add/",
        {
            "username": f"acceptance_{name}",
            "email": f"acceptance_{name}@example.invalid",
            "usable_password": "true",
            "password1": password,
            "password2": password,
            "client": client.pk,
            "_save": "Save",
        },
    )
    user = User.objects.filter(username=f"acceptance_{name}").first()
    run.check(
        response.status_code == 302 and user is not None and user.client_id == client.pk,
        f"Client account acceptance_{name} created for {client.name} (HTTP {response.status_code})",
        "302" + (f"; form said: {form_errors(response)}" if response.status_code == 200 else ""),
    )
    return user


@step(
    "O13",
    "Every change is traceable",
    outcome=13,
    requirement="Every change to a result, status, limit or report is traceable: who, when, old "
    "value, new value, reason.",
    expected="Audit events exist, each with an actor and time, for every kind of change made in "
    "this run; a QC limit change records old and new value and its reason; the result correction "
    "and the amendment record their reasons, and the earlier revision keeps the old value.",
    depends=("O11",),
)
def traceability(run):
    expected = [
        "client.created",
        "project.created",
        "order.created",
        "sample.received",
        "batch.created",
        "qc.sample_added",
        "qc.measured",
        "result.entered",
        "result.submitted",
        "result.returned",
        "result.corrected",
        "result.approved",
        "qc.corrective_action",
        "report.created",
        "report.released",
        "report.amended",
        "account.created",
    ]
    counts = {a: AuditEvent.objects.filter(action=a).count() for a in expected}
    missing = [a for a, n in counts.items() if not n]
    run.check(
        not missing,
        "Audit events by action: " + ", ".join(f"{a} {n}" for a, n in counts.items()),
        f"none missing (missing: {missing})",
    )
    unattributed = (
        AuditEvent.objects.filter(actor__isnull=True).count()
        + AuditEvent.objects.filter(occurred_at__isnull=True).count()
    )
    run.check(
        unattributed == 0,
        f"Every one of {AuditEvent.objects.count()} audit events names its actor and time",
    )

    limit = QCLimit.objects.get(
        definition__method__code=METALS,
        definition__matrix="water",
        definition__analyte__name="Lead",
        phase="analysis",
        qc_type="lcs",
        measure="recovery",
    )
    old = limit.upper
    limit.upper = Decimal("120")
    reason = "Synthetic: acceptance run demonstrates a recorded limit change."
    bench.save_config(actor=account(run, "admin"), instance=limit, reason=reason)
    event = AuditEvent.objects.filter(
        action="config.qclimit.updated", object_id=str(limit.pk)
    ).last()
    run.check(
        event is not None
        and Decimal(event.before["upper"]) == old
        and Decimal(event.after["upper"]) == Decimal("120")
        and event.reason == reason
        and event.actor.username == "acceptance_admin",
        f"QC limit change: upper {event.before['upper'] if event else '?'} -> "
        f"{event.after['upper'] if event else '?'} by {event.actor.username if event else '?'} at "
        f"{timezone.localtime(event.occurred_at).isoformat() if event else '?'}, reason '{reason}'",
    )
    correction = AuditEvent.objects.filter(
        action="result.corrected", after__analyte="Zinc", after__sample=run["sample_a1"].lab_id
    ).last()
    first = ResultRevision.objects.get(
        result__requested_test__sample=run["sample_a1"], analyte_name="Zinc", number=1
    )
    run.check(correction is not None, "The zinc correction has an audit event")
    run.check(
        correction.reason
        and correction.after["supplied_text"] == "46"
        and first.supplied_text == "48",
        f"Zinc correction by {correction.actor.username}: new value "
        f"{correction.after['supplied_text']} with reason '{correction.reason}'; "
        f"revision 1 still holds {first.supplied_text}",
    )
    amended = AuditEvent.objects.filter(action="report.amended").last()
    run.check(
        amended.reason and amended.after.get("changes"),
        f"report.amended by {amended.actor.username}: reason '{amended.reason}', "
        f"changes {amended.after['changes']}",
    )
