"""The controls the first journey rests on, each checked on its own.

Authorization is checked through HTTPS requests to the real URLs, so a refusal comes from the
server, not from a hidden button. The history checks write to history tables directly on
purpose, to show that the database itself refuses to update or delete them.
"""

import hashlib
import json
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from accounts.models import User
from accounts.roles import can_authorize_release
from analytical import services as bench
from analytical.holding import duration_label
from analytical.models import HoldingTimeRule, ResultRevision
from catalog.models import Method
from core.models import AuditEvent
from receipt import services as receipt
from receipt.models import Client, CustodyEvent, Sample
from reporting.content import assemble
from reporting.models import Report, ReportFile, ReportVersion
from reporting.services import create_report, release_report

from .journey import (
    NUTRIENTS,
    VERSION,
    account,
    create_client_user,
    download,
    enter,
    sha256,
    signed_in,
)
from .runner import csrf_refusal, role_refusal, step
from .scratch import sqlstate

# Each case: (who is refused, method, path, why; who may, the status that shows they may).
# A refusal only counts when the same request succeeds for the right person.
REFUSALS = [
    (None, "get", "/samples/", "anonymous: sent to sign-in", "custodian", 200),
    (None, "get", "/reports/{report}/v1.pdf", "anonymous: no download", "manager", 200),
    ("custodian", "get", "/analytical/batches/new/", "custodian builds no batches", "analyst", 200),
    ("manager", "get", "/analytical/batches/new/", "manager builds no batches", "analyst", 200),
    ("reviewer", "get", "/orders/new/", "reviewer opens no custody", "custodian", 200),
    ("analyst", "get", "/clients/new/", "analyst creates no clients", "manager", 200),
    # The approver passes authorization and is then refused for the dummy content hash (409).
    ("analyst", "post", "/reports/{report}/release/", "analyst cannot release", "approver", 409),
    ("custodian", "get", "/admin/accounts/user/", "a role opens no account admin", "admin", 200),
    ("client_a", "get", "/samples/", "client: no staff sample list", "custodian", 200),
    ("client_a", "get", "/reports/{report}/v1.pdf", "client: no staff download", "manager", 200),
]


def request(run, who, method, path):
    browser = signed_in(run, who) if who else run.browser()
    if method == "get":
        response = browser.get(path)
    else:
        response = browser.post(
            path, {"fingerprint": "0" * 64, "confirm": "on", "reason": "Synthetic attempt"}
        )
    location = response.get("Location", "")
    if who and response.status_code == 302 and location.startswith("/admin/login/"):
        response = browser.get(location)  # the admin's sign-in hand-off decides
    return response


@step(
    "C01",
    "Role refusals on the server",
    requirement="Authorize every server-side read and mutation: each role reaches only its own "
    "work, whatever the interface shows (docs/architecture.md, application boundaries; "
    "outcomes 9 and 12).",
    expected="Each request outside the account's role is refused by the server: anonymous "
    "visitors are sent to sign-in (302), signed-in accounts get the authorization 403 (not a "
    "CSRF failure); the same request succeeds for an account whose role permits it.",
    depends=("O12",),
)
def role_refusals(run):
    report = run["report_a"].pk
    for who, method, template, why, allowed, status in REFUSALS:
        path = template.format(report=report)
        response = request(run, who, method, path)
        if who is None:
            refused = response.status_code == 302 and response["Location"].startswith(
                "/accounts/login/"
            )
        else:
            refused = role_refusal(response)
        permitted = request(run, allowed, method, path)
        run.check(
            refused and permitted.status_code == status,
            f"{who or 'anonymous'} {method.upper()} {path}: HTTP {response.status_code} ({why}); "
            f"acceptance_{allowed}: HTTP {permitted.status_code}",
            f"{'302 to sign-in' if who is None else 'authorization 403'}, and {status} for "
            f"acceptance_{allowed}",
        )
    run.check(
        ReportVersion.objects.filter(report_id=report).count() == 2,
        "No request in this step released anything",
    )


@step(
    "C02",
    "Client isolation by direct address",
    requirement="A client account sees only its own records, even when it asks for another "
    "client's report by its address (outcome 12).",
    expected="Each client account opens its own report page and PDF (HTTP 200, stored bytes) "
    "and gets 404 for the other client's report page and PDF at the same addresses; its report "
    "list holds only its own report.",
    depends=("O12",),
)
def client_isolation(run):
    approver = account(run, "approver")
    report_b = create_report(
        actor=account(run, "manager"),
        project=run["project_b"],
        title="Reservoir acceptance report (synthetic)",
        samples=[run["sample_b1"]],
    )
    release_report(
        actor=approver,
        report_id=report_b.pk,
        expected_fingerprint=assemble(
            Report.objects.get(pk=report_b.pk), actor=approver
        ).fingerprint,
    )
    create_client_user(run, "client_b", run["project_b"].client)
    report_a = run["report_a"]
    for who, own, other in (("client_a", report_a, report_b), ("client_b", report_b, report_a)):
        browser = signed_in(run, who)
        stored = bytes(ReportFile.objects.get(version__report=own, version__number=1).pdf)
        page = browser.get(f"/portal/reports/{own.pk}/")
        pdf = browser.get(f"/portal/reports/{own.pk}/v1.pdf")
        run.check(
            page.status_code == 200 and pdf.status_code == 200 and pdf.content == stored,
            f"acceptance_{who} opens its own {own.number}: page HTTP {page.status_code}, "
            f"v1.pdf HTTP {pdf.status_code} with the stored bytes",
            "200 and 200",
        )
        for path in (f"/portal/reports/{other.pk}/", f"/portal/reports/{other.pk}/v1.pdf"):
            response = browser.get(path)
            run.check(
                response.status_code == 404,
                f"acceptance_{who} GET {path} ({other.number}, the other client's): "
                f"HTTP {response.status_code}",
                "404",
            )
        listing = browser.get("/portal/reports/").content.decode("utf-8", "replace")
        run.check(
            own.number in listing and other.number not in listing,
            f"acceptance_{who} report list shows {own.number}, not {other.number}",
        )


@step(
    "C03",
    "Nobody reviews their own results",
    requirement="A qualified person other than the analyst performs technical review; nobody "
    "reviews their own results, even when holding both roles (outcome 8).",
    expected="An account with both analyst and reviewer roles enters and submits a result. Its "
    "own approval and return are refused by the service, and through the review page each is "
    "answered with the page's own refusal (redirect back, warning shown) while the result stays "
    "submitted. The independent reviewer's return through the same page succeeds.",
    depends=("O03",),
)
def independent_review(run):
    dual = account(run, "analyst_reviewer")
    sample = run["sample_a3"]
    batch = bench.create_batch(
        actor=dual,
        method=Method.objects.get(code=NUTRIENTS, version=VERSION),
        matrix="water",
        prepared_at=timezone.now() - timedelta(minutes=40),
    )
    bench.add_tests_to_batch(
        actor=dual,
        batch_id=batch.pk,
        requested_test_ids=list(sample.requested_tests.values_list("pk", flat=True)),
    )
    revision = enter(run, sample, "Nitrate as N", "1.20", rl="0.05", who="analyst_reviewer")
    bench.submit_result(actor=dual, revision_id=revision.pk)
    for decide in (
        lambda: bench.approve_result(actor=dual, revision_id=revision.pk),
        lambda: bench.return_result(actor=dual, revision_id=revision.pk, reason="Own work"),
    ):
        try:
            decide()
            refusal = None
        except PermissionDenied as exc:
            refusal = str(exc)
        run.check(
            refusal is not None and "you cannot review it" in refusal,
            f"Service refused acceptance_analyst_reviewer: '{refusal}'",
            "the independence refusal",
        )
    page = f"/analytical/review/batches/{batch.pk}/"
    browser = signed_in(run, "analyst_reviewer")
    for action, note in (("approve", ""), ("return", "Synthetic: own work, returned")):
        response = browser.post(page, {"action": action, "note": note, "revisions": [revision.pk]})
        shown = browser.get(page).content.decode("utf-8", "replace")
        warned = "someone else must review work you entered or submitted" in shown
        back = response.get("Location") == page
        revision.refresh_from_db()
        run.check(
            response.status_code == 302 and back and warned and revision.status == "submitted",
            f"{action.capitalize()} of own work through the review page: HTTP "
            f"{response.status_code}, {'back to the page' if back else 'not back to the page'}, "
            f"independence warning {'shown' if warned else 'not shown'}; result "
            f"'{revision.status}'",
            "the page's own refusal and a submitted result",
        )
    response = signed_in(run, "reviewer").post(
        page,
        {"action": "return", "note": "Synthetic: independent return.", "revisions": [revision.pk]},
    )
    revision.refresh_from_db()
    run.check(
        response.status_code == 302 and revision.status == "returned",
        f"The same return by acceptance_reviewer: HTTP {response.status_code}, result "
        f"'{revision.status}'",
        "returned",
    )


@step(
    "C04",
    "Release needs explicit permission",
    requirement="Release is authorized only by an account holding the explicit release "
    "permission; an administrator or reviewer role alone does not grant it (outcome 9).",
    expected="The administrator and the reviewer are refused by the release service and by the "
    "release URL (authorization 403, not a CSRF failure); the approver's identical request "
    "passes authorization and is refused only for its stale content hash (409); only the "
    "approver group carries the permission.",
    depends=("O09",),
)
def release_permission(run):
    report = Report.objects.get(pk=run["report_a"].pk)
    for who in ("admin", "reviewer"):
        user = account(run, who)
        try:
            release_report(actor=user, report_id=report.pk, expected_fingerprint="0" * 64)
            refusal = None
        except PermissionDenied as exc:
            refusal = str(exc)
        run.check(
            refusal is not None and "release permission" in refusal,
            f"Release service refused acceptance_{who}: '{refusal}'",
            "the release-permission refusal",
        )
    attempt = {"fingerprint": "0" * 64, "confirm": "on", "reason": "Synthetic attempt"}
    path = f"/reports/{report.pk}/release/"
    for who in ("admin", "reviewer"):
        response = signed_in(run, who).post(path, attempt)
        run.check(
            role_refusal(response),
            f"Release URL for acceptance_{who}: HTTP {response.status_code}",
            "authorization 403",
        )
    response = signed_in(run, "approver").post(path, attempt)
    run.check(
        response.status_code == 409,
        f"The same request by acceptance_approver: HTTP {response.status_code} (authorized; "
        "refused for the stale content hash)",
        "409",
    )
    accounts = User.objects.filter(username__startswith="acceptance_", client__isnull=True)
    holders = sorted(u.username for u in accounts if can_authorize_release(u))
    run.check(
        holders == ["acceptance_approver"],
        f"Staff accounts holding release permission: {holders}; the administrator, a Django "
        "superuser, holds none",
        "['acceptance_approver']",
    )


@step(
    "C05",
    "Holding time computed from the rule",
    requirement="Expiry is computed from the configured rule (start event, end event, duration), "
    "never typed; a result past its limit is flagged and needs a holding-time qualifier "
    "(outcome 4; 40 CFR 136.3 Table II footnote 4 as cited in docs/product.md).",
    expected="Changing the rule duration moves the deadline shown for an unanalysed sample; a "
    "result analysed after the deadline is evaluated as exceeded and cannot be submitted without "
    "the H qualifier; with it, it is submitted.",
    depends=("O03",),
)
def holding_rule(run):
    sample = Sample.objects.get(pk=run["sample_a4"].pk)
    admin = account(run, "admin")
    rule = HoldingTimeRule.objects.get(
        method__code=NUTRIENTS, method__version=VERSION, matrix="water", start_event="collection"
    )
    original = rule.duration
    browser = signed_in(run, "custodian")
    for duration in (original * 2, original):
        rule.duration = duration
        bench.save_config(
            actor=admin,
            instance=rule,
            reason=f"Synthetic: acceptance run sets the rule to {duration}.",
        )
        deadline = timezone.localtime(sample.collected_at + duration).isoformat()
        html = browser.get(f"/samples/{sample.pk}/").content.decode("utf-8", "replace")
        run.check(
            f'datetime="{deadline}"' in html,
            f"Rule set to {duration_label(duration)}: sample page deadline {deadline}",
        )
    analyst = account(run, "analyst")
    batch = bench.create_batch(
        actor=analyst,
        method=rule.method,
        matrix="water",
        prepared_at=timezone.now() - timedelta(minutes=30),
    )
    bench.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=list(sample.requested_tests.values_list("pk", flat=True)),
    )
    revision = enter(run, sample, "Nitrate as N", "0.80", rl="0.05")
    run.check(
        revision.holding_status == "exceeded",
        f"Result analysed {timezone.localtime(revision.analysis_started_at):%Y-%m-%d %H:%M %z} "
        f"evaluated as '{revision.holding_status}': "
        + "; ".join(e.get("detail", "") for e in revision.holding_evaluation),
        "exceeded",
    )
    try:
        bench.submit_result(actor=analyst, revision_id=revision.pk)
        refusal = None
    except ValidationError as exc:
        refusal = " ".join(exc.messages)
    run.check(
        refusal is not None and "holding-time qualifier" in refusal,
        f"Submission without a holding-time qualifier refused: '{refusal}'",
        "the holding-time refusal",
    )
    qualified = enter(
        run,
        sample,
        "Nitrate as N",
        "0.80",
        rl="0.05",
        qualifiers=("H",),
        reason="Synthetic: holding time exceeded; qualified.",
    )
    bench.submit_result(actor=analyst, revision_id=qualified.pk)
    qualified.refresh_from_db()
    run.check(
        qualified.status == "submitted" and [q["code"] for q in qualified.qualifiers] == ["H"],
        f"Revision {qualified.number} with qualifier H submitted; revision {revision.number} kept",
    )


def guarded_rows(run):
    """(label, table, key column, key, column, new value) of history rows from this run."""
    v1 = ReportVersion.objects.get(report=run["report_a"], number=1)
    released = AuditEvent.objects.get(action="report.released", object_id=str(v1.pk))
    custody = CustodyEvent.objects.filter(sample=run["sample_a1"]).first()
    first = ResultRevision.objects.get(
        result__requested_test__sample=run["sample_a1"], analyte_name="Zinc", number=1
    )
    return [
        ("issued report PDF", ReportFile, "version_id", v1.pk, "pdf", b"%PDF-forged"),
        ("issued report version", ReportVersion, "id", v1.pk, "amendment_reason", "edited"),
        ("audit event", AuditEvent, "id", released.pk, "reason", "edited"),
        ("custody event", CustodyEvent, "id", custody.pk, "reason", "edited"),
        ("result observation", ResultRevision, "id", first.pk, "supplied_text", "999"),
    ]


# The history triggers raise with these SQLSTATEs (raise_exception, check_violation) and a
# message saying the row "cannot be" changed; only the SQLSTATE is written to the evidence.
TRIGGER_STATES = {"P0001", "23514"}


def refused_by_database(statement, params):
    """Run one SQL statement in a savepoint. Returns (SQLSTATE, whether the message is the
    history trigger's) for a refusal, or None when the statement went through."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(statement, params)
    except DatabaseError as exc:
        return sqlstate(exc), "cannot be" in str(exc)
    return None


@step(
    "C06",
    "Issued reports and history cannot be changed",
    requirement="Issued report versions are immutable: later edits to the records do not change "
    "an issued PDF or snapshot, and the database itself refuses to update or delete history rows "
    "(outcomes 10, 11 and 13).",
    expected="After the client and project are edited with a reason, version 1's PDF and snapshot "
    "are unchanged; direct UPDATE and DELETE of the issued PDF, version, audit, custody and result "
    "rows are refused by the database; version 1 still downloads byte for byte.",
    depends=("O11",),
)
def immutability(run):
    manager = account(run, "manager")
    client = Client.objects.get(pk=run["client_a"].pk)
    receipt.update_client(
        actor=manager,
        client_id=client.pk,
        expected_revision=client.revision,
        reason="Synthetic: client moved after the report was issued.",
        address_line1="200 Changed Street",
    )
    project = run["project_a"].__class__.objects.get(pk=run["project_a"].pk)
    receipt.update_project(
        actor=manager,
        project_id=project.pk,
        expected_revision=project.revision,
        reason="Synthetic: permit label corrected after issue.",
        program="SYNTH-PERMIT-0002",
    )
    run.observe("Client address and project permit label changed with reasons after issue")
    stored = ReportFile.objects.get(version__report=run["report_a"], version__number=1)
    snapshot = hashlib.sha256(json.dumps(stored.snapshot, sort_keys=True).encode()).hexdigest()
    run.check(
        bytes(stored.pdf) == run["v1_pdf"] and snapshot == run["v1_snapshot"],
        f"v1 stored PDF and snapshot unchanged (snapshot SHA-256 {snapshot[:16]}…; it still prints "
        f"'{stored.snapshot['content'].get('project', {}).get('program', '')}')",
    )
    quote = connection.ops.quote_name
    for label, model, key, value, column, changed in guarded_rows(run):
        table, key, column = quote(model._meta.db_table), quote(key), quote(column)
        for action, statement, params in (
            ("UPDATE", f"UPDATE {table} SET {column} = %s WHERE {key} = %s", [changed, value]),
            ("DELETE", f"DELETE FROM {table} WHERE {key} = %s", [value]),
        ):
            refusal = refused_by_database(statement, params)
            run.check(
                refusal is not None and refusal[0] in TRIGGER_STATES and refusal[1],
                f"SQL {action} of the {label}: "
                + (
                    f"refused (SQLSTATE {refusal[0]}, "
                    f"{'the history trigger' if refusal[1] else 'not the history trigger'})"
                    if refusal
                    else "not refused"
                ),
                "refused by the history trigger",
            )
    response, body = download(run, "client_a", f"/portal/reports/{run['report_a'].pk}/v1.pdf")
    run.check(
        body == run["v1_pdf"], f"v1 downloads byte for byte as issued (SHA-256 {sha256(body)})"
    )
    events = AuditEvent.objects.filter(
        action__in=["client.updated", "project.updated"], reason__startswith="Synthetic"
    )
    run.check(
        events.count() == 2,
        f"{events.count()} audit events record the two edits with their reasons",
        "2",
    )


@step(
    "C07",
    "Forged form posts are refused",
    requirement="State-changing requests need the CSRF token and a matching origin "
    "(architecture: CSRF protection, explicit trusted origins).",
    expected="A signed-in project manager's form post without the token, and one with the token "
    "from a foreign origin, are both refused by the CSRF check (403) and create nothing; the "
    "same post with the token from the deployment's own origin creates the client.",
    depends=("O01",),
)
def csrf(run):
    browser = signed_in(run, "manager")
    data = {"name": "Forged Client (synthetic)"}
    without = browser.post("/clients/new/", data, csrf=False)
    foreign = browser.post("/clients/new/", data, origin="https://attacker.example.invalid")
    run.check(
        csrf_refusal(without)
        and csrf_refusal(foreign)
        and not Client.objects.filter(name="Forged Client (synthetic)").exists(),
        f"Without token: HTTP {without.status_code}; foreign origin: HTTP {foreign.status_code}; "
        "both the CSRF failure page; no client created",
        "the CSRF 403 twice",
    )
    genuine = browser.post("/clients/new/", data)
    run.check(
        genuine.status_code == 302 and Client.objects.filter(name=data["name"]).exists(),
        f"The same post with the token from the own origin: HTTP {genuine.status_code}, client "
        "created",
        "302",
    )
