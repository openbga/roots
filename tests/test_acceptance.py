"""The acceptance command: scratch-database safety (refusals, guards, cleanup, locks), step
isolation, the evidence package and its scrubbing, sign-in behind each proxy setting, and one
complete run of every acceptance step in the test database.

Databases these tests create beside the test database are named `acceptance_<test db>...` and
dropped in a finalizer even when a test fails. Only one test migrates a database for real.
"""

import copy
import hashlib
import json
import os
from contextlib import contextmanager
from urllib.parse import quote

import pytest
from django.conf import settings
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db import connection
from django.test.utils import override_settings

from accounts.models import User
from core.acceptance import evidence, runner, scratch
from core.acceptance.runner import (
    FAILED,
    NOT_RUN,
    PASSED,
    Browser,
    Run,
    Step,
    load_steps,
    run_steps,
)
from core.management.commands import run_acceptance as command
from receipt.models import Client

OPERATOR = "Example Operator (synthetic)"


# ------------------------------------------------------------------------------ helpers


def working():
    return copy.deepcopy(dict(connection.settings_dict))


def url_for(name, host=None):
    db = connection.settings_dict
    user = quote(db.get("USER") or "", safe="")
    password = quote(db.get("PASSWORD") or "", safe="")
    credentials = f"{user}:{password}@" if password else (f"{user}@" if user else "")
    address = f"{host or db.get('HOST') or 'localhost'}:{db.get('PORT') or 5432}"
    return f"postgresql://{credentials}{address}/{name}"


def server_sql(statement, params=None, *, database=None):
    target = scratch.target_settings(working(), database) if database else working()
    with scratch.opened(target, "test") as wrapper:
        return scratch.run_sql(wrapper, statement, params)


def exists(name):
    return bool(server_sql("SELECT 1 FROM pg_database WHERE datname = %s", [name]))


def drop(name):
    server_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def disposable():
    """Names of databases a test creates; each is dropped afterwards, also on failure."""
    names = []
    yield names
    for name in names:
        drop(name)


def create(names, name, *, marked=False, tables=()):
    names.append(name)
    drop(name)
    server_sql(f'CREATE DATABASE "{name}" TEMPLATE template0')
    with scratch.opened(scratch.target_settings(working(), name), "test") as wrapper:
        if marked:
            scratch.mark(wrapper)
        for table in tables:
            scratch.run_sql(wrapper, f"CREATE TABLE {table} (id int)")
    return name


def run_command(tmp_path, *args, output="out"):
    with pytest.raises(CommandError) as raised:
        call_command(
            "run_acceptance", "--operator", OPERATOR, "--output", str(tmp_path / output), *args
        )
    return raised.value


def call_ok(tmp_path, *args):
    call_command("run_acceptance", "--operator", OPERATOR, "--output", str(tmp_path / "out"), *args)
    return json.loads((tmp_path / "out" / "evidence.json").read_text())


def marker_in_working():
    return server_sql("SELECT to_regclass(%s) IS NOT NULL", [scratch.MARKER])[0][0]


def on_working_database(name):
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        return cursor.fetchone()[0] == name and connection.settings_dict["NAME"] == name


def check_in_scratch(run):
    """A fake step: the run's own connection is on a scratch database, not the working one."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        name = cursor.fetchone()[0]
    run.check(name.startswith("acceptance_"), f"running in {name}")


def check_migrated(run):
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('core_lab') IS NOT NULL")
        run.check(cursor.fetchone()[0], "the scratch database was migrated")


SCRATCH_STEPS = [
    Step("T01", "In scratch", "R.", "E.", None, (), check_in_scratch),
]


@pytest.fixture
def fake_run(monkeypatch):
    """Real scratch handling with two cheap steps and no migration (one test migrates)."""
    monkeypatch.setattr(command, "load_steps", lambda: SCRATCH_STEPS)
    monkeypatch.setattr(scratch, "call_command", lambda *a, **k: None)
    monkeypatch.setattr(evidence, "render_pdf", lambda html: b"%PDF-1.7 synthetic test")


# ------------------------------------------------------------------------ refusals


@pytest.mark.django_db
@pytest.mark.parametrize("host", [None, "localhost", "127.0.0.1"])
def test_refuses_the_working_database_however_it_is_named(tmp_path, host):
    error = run_command(
        tmp_path, "--scratch-database-url", url_for(connection.settings_dict["NAME"], host)
    )
    assert error.returncode == 2
    assert "working database" in str(error) and "Nothing was changed" in str(error)
    assert not (tmp_path / "out").exists()
    assert not marker_in_working()


@pytest.mark.django_db
def test_refuses_a_scratch_database_that_holds_anything_else(tmp_path, disposable):
    name = create(disposable, scratch.scratch_name(f"{connection.settings_dict['NAME']}_supplied"))

    def state():
        with scratch.opened(scratch.target_settings(working(), name), "t") as wrapper:
            return scratch.describe(wrapper)[0]

    assert state() == "empty"
    server_sql("CREATE TABLE laboratory_records (id int)", database=name)
    error = run_command(tmp_path, "--scratch-database-url", url_for(name))
    assert error.returncode == 2 and "not empty" in str(error)
    assert server_sql("SELECT to_regclass('laboratory_records') IS NOT NULL", database=name)[0][0]

    # The marker of an earlier run makes it reusable, but only in the current schema and only
    # while it holds no other laboratory.
    server_sql("CREATE SCHEMA elsewhere", database=name)
    server_sql(f"CREATE TABLE elsewhere.{scratch.MARKER} (note text)", database=name)
    assert state() == "other"
    server_sql(f"CREATE TABLE {scratch.MARKER} (note text)", database=name)
    assert state() == "acceptance"
    server_sql("CREATE TABLE core_lab (name text)", database=name)
    server_sql("INSERT INTO core_lab VALUES ('Example Environmental Lab')", database=name)
    assert state() == "other"


@pytest.mark.django_db
def test_reports_a_leftover_scratch_database_and_keeps_it(tmp_path, disposable):
    name = create(disposable, scratch.scratch_name(connection.settings_dict["NAME"]), marked=True)
    error = run_command(tmp_path)
    assert error.returncode == 2 and "--drop-stale-scratch-database" in str(error)
    assert exists(name)


@pytest.mark.django_db
def test_refuses_while_another_run_holds_the_lock(tmp_path):
    name = scratch.scratch_name(connection.settings_dict["NAME"])
    with scratch.opened(working(), "other-run") as other:
        identity = scratch.read_identity(other)
        scratch.run_sql(other, "SELECT pg_advisory_lock(%s)", [scratch.lock_key(identity[0], name)])
        error = run_command(tmp_path)
    assert error.returncode == 2 and "Another acceptance run" in str(error)
    assert not exists(name)


@pytest.mark.django_db
def test_refuses_an_output_directory_that_is_not_empty_or_is_served(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "earlier.txt").write_text("earlier evidence")
    assert run_command(tmp_path).returncode == 2
    with pytest.raises(CommandError, match="which the application serves") as raised:
        call_command(
            "run_acceptance", "--operator", OPERATOR, "--output", str(settings.STATIC_ROOT / "x")
        )
    assert raised.value.returncode == 2


# ------------------------------------------------------------- real scratch handling


@pytest.mark.django_db(transaction=True)
def test_created_mode_creates_migrates_and_drops(tmp_path, monkeypatch):
    working_name = connection.settings_dict["NAME"]
    steps = [*SCRATCH_STEPS, Step("T02", "Migrated", "R.", "E.", None, (), check_migrated)]
    monkeypatch.setattr(command, "load_steps", lambda: steps)
    monkeypatch.setattr(evidence, "render_pdf", lambda html: b"%PDF-1.7 synthetic test")
    try:
        record = call_ok(tmp_path)
    finally:
        drop(scratch.scratch_name(working_name))
    assert record["summary"]["all_passed"] and record["database"]["after_run"] == "dropped"
    assert not exists(scratch.scratch_name(working_name))
    assert on_working_database(working_name) and not marker_in_working()


@pytest.mark.django_db(transaction=True)
def test_dbname_in_options_never_reaches_the_working_database(tmp_path, monkeypatch, fake_run):
    """`?dbname=` in DATABASE_URL lands in OPTIONS, where it would override NAME."""
    working_name = connection.settings_dict["NAME"]
    monkeypatch.setitem(connection.settings_dict, "OPTIONS", {"dbname": working_name})
    try:
        record = call_ok(tmp_path)
    finally:
        drop(scratch.scratch_name(working_name))
    assert record["summary"]["all_passed"]
    assert not marker_in_working() and on_working_database(working_name)


@pytest.mark.django_db(transaction=True)
def test_a_tripped_guard_cleans_up_nothing(tmp_path, monkeypatch, fake_run, disposable):
    working_name = connection.settings_dict["NAME"]
    name = scratch.scratch_name(working_name)
    disposable.append(name)

    def misdirected(settings_dict, scratch_name):  # NAME says scratch; OPTIONS says working
        target = copy.deepcopy(settings_dict)
        target["NAME"] = scratch_name
        target["OPTIONS"] = {**(target.get("OPTIONS") or {}), "dbname": working_name}
        return target

    monkeypatch.setattr(scratch, "target_settings", misdirected)
    error = run_command(tmp_path)
    assert error.returncode == 2 and f"connected to database {working_name!r}" in str(error)
    assert exists(name)  # created, then left alone once the guard tripped
    assert not marker_in_working() and on_working_database(working_name)


@pytest.mark.django_db(transaction=True)
def test_an_error_inside_the_run_still_cleans_up(tmp_path, monkeypatch, fake_run):
    working_name = connection.settings_dict["NAME"]

    def explode(*args, **kwargs):
        raise RuntimeError("synthetic failure inside the run")

    monkeypatch.setattr(command, "run_steps", explode)
    try:
        error = run_command(tmp_path)
    finally:
        drop(scratch.scratch_name(working_name))
    assert error.returncode == 3 and "RuntimeError" in str(error)
    assert not exists(scratch.scratch_name(working_name))
    assert on_working_database(working_name) and not (tmp_path / "out").exists()


@pytest.mark.django_db(transaction=True)
def test_drop_stale_removes_a_leftover_and_runs(tmp_path, fake_run, disposable):
    name = create(disposable, scratch.scratch_name(connection.settings_dict["NAME"]), marked=True)
    server_sql("CREATE TABLE leftover_rows (id int)", database=name)
    record = call_ok(tmp_path, "--drop-stale-scratch-database")
    assert record["summary"]["all_passed"] and not exists(name)


@pytest.mark.django_db(transaction=True)
def test_supplied_database_left_by_an_earlier_run_is_reused_and_emptied(
    tmp_path, fake_run, disposable
):
    name = scratch.scratch_name(f"{connection.settings_dict['NAME']}_supplied")
    create(disposable, name, marked=True, tables=("leftover_rows",))
    record = call_ok(tmp_path, "--scratch-database-url", url_for(name))
    assert record["summary"]["all_passed"] and record["database"]["after_run"] == "emptied"
    assert record["database"]["mode"] == "supplied"
    tables = server_sql(
        "SELECT count(*) FROM pg_tables WHERE schemaname = current_schema()", database=name
    )
    assert tables[0][0] == 0


# --------------------------------------------------------------------- the runner


def passing(run):
    run.check(True, "first observation")


def failing(run):
    Client.objects.create(name="Half-done Client (synthetic)")
    run.check(False, "observed something else", "the expected thing")


def erroring(run):
    raise RuntimeError("could not connect")


FAKE_STEPS = [
    Step("T01", "Passes", "Requirement one.", "It passes.", 1, (), passing),
    Step("T02", "Fails", "Requirement two.", "It fails.", 2, ("T01",), failing),
    Step("T03", "Depends on a failure", "Requirement three.", "Not run.", None, ("T02",), passing),
    Step("T04", "Raises", "Requirement four.", "It errors.", None, (), erroring),
    Step("T05", "Independent", "Requirement five.", "It passes.", None, ("T01",), passing),
    Step("T06", "Depends on not run", "Requirement six.", "Not run.", None, ("T03",), passing),
]


@pytest.mark.django_db
def test_failures_stay_with_their_step_and_dependents_are_not_run():
    results = {r.step.id: r for r in run_steps(FAKE_STEPS, Run())}
    assert {k: r.status for k, r in results.items()} == {
        "T01": PASSED,
        "T02": FAILED,
        "T03": NOT_RUN,
        "T04": FAILED,
        "T05": PASSED,
        "T06": NOT_RUN,
    }
    assert not Client.objects.filter(name="Half-done Client (synthetic)").exists()  # rolled back
    assert "expected: the expected thing" in results["T02"].observations[-1]
    assert "T02 (failed)" in results["T03"].observations[0]
    assert "RuntimeError" in results["T04"].observations[0]


@contextmanager
def _test_database(**kwargs):  # the test database stands in for the scratch database
    yield {
        "mode": "created",
        "name": "acceptance_example",
        "applied_migrations": [],
        "kept": False,
        "cleanup": "dropped",
    }


SECRET_KEY = "k&<'"  # short and with characters HTML escapes
RUN_PASSWORD = "pw&<'\"x"
LEAKS = [
    "POSTGRESQL://someone:hunter2hunter2@db.example.invalid/x",
    "mysql://someone:hunter3@db.example.invalid/x",
    "host=db.example.invalid password=hunter4 dbname=x",
    f"the run password {RUN_PASSWORD}",
    f"the key {SECRET_KEY}",
]
FORBIDDEN = ["hunter2hunter2", "hunter3", "hunter4", "db.example.invalid", RUN_PASSWORD, SECRET_KEY]


def leaking(run):
    for text in LEAKS:
        run.observe(text)


def database_error(run):
    from django.db import OperationalError

    raise OperationalError('connection to server at "db.example.invalid" (172.18.0.2) failed')


@pytest.mark.django_db
def test_failed_run_writes_a_complete_scrubbed_package(tmp_path, monkeypatch, settings):
    settings.SECRET_KEY = SECRET_KEY
    monkeypatch.setattr(scratch, "scratch_database", _test_database)
    steps = [
        *FAKE_STEPS,
        Step("T07", "Leaks", "R.", "E.", None, (), leaking),
        Step("T08", "Database error", "R.", "E.", None, (), database_error),
    ]
    monkeypatch.setattr(command, "load_steps", lambda: steps)
    original_run = command.Run

    def run_with_secret():
        run = original_run()
        run.secrets.append(RUN_PASSWORD)
        return run

    monkeypatch.setattr(command, "Run", run_with_secret)
    error = run_command(tmp_path)
    assert error.returncode == 1 and "Not every step passed" in str(error)

    out = tmp_path / "out"
    record = json.loads((out / "evidence.json").read_text())
    assert record["summary"] == {
        "passed": 3,
        "failed": 3,
        "not_run": 2,
        "total": 8,
        "all_passed": False,
    }
    assert record["operator"] == OPERATOR and record["problems"] == []
    assert "OperationalError (SQLSTATE unknown)" in record["steps"][-1]["observed"][-1]
    html = (out / "report.html").read_text()
    assert "3 of 8 steps failed" in html and "Performed by" in html and "Reviewed by" in html
    assert "It is not a statement that the software or the laboratory is validated" in html
    assert "The laboratory decides what this record means in its quality system" in html

    files = {p.relative_to(out).as_posix(): p for p in out.rglob("*") if p.is_file()}
    manifest = dict(
        reversed(line.split("  ", 1))
        for line in (out / "manifest-sha256.txt").read_text().splitlines()
    )
    assert set(manifest) == set(files) - {"manifest-sha256.txt"}
    for name, digest in manifest.items():
        assert hashlib.sha256(files[name].read_bytes()).hexdigest() == digest

    from django.utils.html import escape

    forbidden = [*FORBIDDEN, *(escape(v) for v in FORBIDDEN), "://", "172.18.0.2"]
    forbidden += scratch.long_enough([connection.settings_dict.get("PASSWORD")])
    for name, path in files.items():
        text = path.read_bytes().decode("utf-8", "replace")
        for value in forbidden:
            assert value not in text, f"{name} contains {value!r}"


def test_short_database_credentials_do_not_garble_the_record(monkeypatch):
    """A short database password or user name is an ordinary word somewhere in the record;
    replacing it everywhere would show a reader what it was. Connection strings still go."""
    from core.acceptance.runner import scrub

    for key, value in (("PASSWORD", "2026"), ("USER", "analyst"), ("HOST", "db")):
        monkeypatch.setitem(connection.settings_dict, key, value)
    text = "Started 2026-11-13; acceptance_analyst signed in; db postgres://analyst:2026@db/lims"
    assert scrub(text, names=["analyst", "db"]) == (
        "Started 2026-11-13; acceptance_analyst signed in; db [connection string withheld]"
    )
    monkeypatch.setitem(connection.settings_dict, "PASSWORD", "long-password-1")
    monkeypatch.setitem(connection.settings_dict, "HOST", "database.example.invalid")
    assert scrub("long-password-1 at database.example.invalid") == "[withheld] at [withheld]"


@pytest.mark.django_db
def test_pdf_failure_leaves_an_incomplete_package_and_exit_1(tmp_path, monkeypatch):
    monkeypatch.setattr(scratch, "scratch_database", _test_database)
    monkeypatch.setattr(command, "load_steps", lambda: FAKE_STEPS[:1])

    def broken(html):
        raise OSError("no PDF libraries")

    monkeypatch.setattr(evidence, "render_pdf", broken)
    error = run_command(tmp_path)
    assert error.returncode == 1 and "report.pdf could not be rendered" in str(error)
    out = tmp_path / "out"
    assert not (out / "report.pdf").exists()
    record = json.loads((out / "evidence.json").read_text())
    assert record["summary"]["all_passed"] and record["problems"]
    assert "report.pdf could not be rendered" in (out / "report.html").read_text()


# ----------------------------------------------------------- sign-in behind a proxy


@pytest.mark.django_db
@pytest.mark.parametrize(
    "proxy",
    [
        {},
        {"ALLAUTH_TRUSTED_CLIENT_IP_HEADER": "X-Real-IP"},
        {"ALLAUTH_TRUSTED_PROXY_COUNT": 1},
        {"ALLAUTH_TRUSTED_PROXY_COUNT": 2},
    ],
)
def test_sign_in_works_under_each_proxy_setting(proxy):
    cache.clear()
    User.objects.create_user("acceptance_probe", "probe@example.invalid", "synthetic-pass-729!x")
    with override_settings(**proxy):
        response = Browser(Run()).sign_in("acceptance_probe", "synthetic-pass-729!x")
        sent = runner.proxy_headers()
    assert response.status_code == 302 and "/accounts/login" not in response["Location"]
    if proxy.get("ALLAUTH_TRUSTED_CLIENT_IP_HEADER"):
        assert sent["X-Real-IP"] == "127.0.0.1"
    if proxy.get("ALLAUTH_TRUSTED_PROXY_COUNT"):
        assert len(sent["X-Forwarded-For"].split(",")) == proxy["ALLAUTH_TRUSTED_PROXY_COUNT"]
    assert sent["X-Forwarded-Proto"] == "https"


# -------------------------------------------------------------------- the whole run


@pytest.mark.django_db
def test_every_acceptance_step_passes():
    """The whole script set, in the test database instead of a scratch database."""
    try:
        import weasyprint  # noqa: F401
    except OSError:
        if os.environ.get("REQUIRE_WEASYPRINT") == "1":
            raise
        pytest.skip("WeasyPrint's native libraries are not installed")
    cache.clear()  # sign-in rate limits count every sign-in of the test session
    results = run_steps(load_steps())
    failures = {r.step.id: r.observations[-1:] for r in results if r.status != PASSED}
    assert not failures
    assert len(results) >= 20
