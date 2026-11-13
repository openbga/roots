"""The separate scratch database an acceptance run works in.

The laboratory's working database is never written. The run connects to it only to read its
migration table, to read its identity and, in created mode, to hold an advisory lock and issue
CREATE DATABASE and DROP DATABASE for the scratch database.

Created mode (default): a database named `acceptance_<working database name>` is created on the
same server (the role needs CREATEDB), marked, migrated from empty and dropped afterwards.
Supplied mode: the operator gives the URL of an empty database whose current schema the role
owns; it is marked, migrated, used and emptied again.

Safety rules:
- Every connection is made by Django's own database backend from a settings dictionary, so a
  helper connects exactly where Django would. Each connection that creates, marks, empties or
  drops first reads the server's identity for itself (database name and OID, server port and
  start time) and checks it against the expected database and against the working database.
- When such a check fails the run stops at once and cleans up nothing; the message says what
  exists. A failed check is a `GuardTripped`, which is never caught here.
- A supplied database is refused when it is the working database, however its URL is written,
  and unless it is empty or carries the marker of an earlier acceptance run (in its current
  schema, holding no other laboratory).
- An advisory lock keyed on the scratch database is held for the whole run, so two runs never
  drop or empty each other's database.
"""

import copy
import hashlib
from contextlib import contextmanager

import dj_database_url
from django.core.management import call_command
from django.db import DatabaseError, connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

MARKER = "acceptance_scratch_marker"
MARKER_NOTE = (
    "Scratch database of manage.py run_acceptance; synthetic records only; safe to remove."
)
IDENTITY = (
    "SELECT current_database(), d.oid, current_setting('port'), pg_postmaster_start_time() "
    "FROM pg_database d WHERE d.datname = current_database()"
)


class Refused(Exception):
    """The run cannot start safely. The message says whether anything was created."""


class GuardTripped(Refused):
    """A connection was not where it had to be. Nothing is cleaned up after this."""


def sqlstate(exc):
    """The SQLSTATE of a database error (Django wraps the driver's error), or None."""
    for error in (exc, exc.__cause__):
        if code := getattr(error, "sqlstate", None):
            return code
    return None


def describe_db_error(exc):
    """Class and SQLSTATE only: driver messages can carry host names and addresses."""
    return f"{type(exc).__name__} (SQLSTATE {sqlstate(exc) or 'unknown'})"


def scratch_name(working_name):
    """`acceptance_<name>`, cut to PostgreSQL's 63-byte identifier limit."""
    name = f"acceptance_{working_name}".encode()[:63]
    return name.decode("utf-8", "ignore")


def target_settings(working, name):
    """Settings for a database beside the working one: only the name differs. A `dbname` in
    OPTIONS would override NAME in Django's connection parameters, so it is left out."""
    target = copy.deepcopy(working)
    target["NAME"] = name
    target["OPTIONS"] = {k: v for k, v in (working.get("OPTIONS") or {}).items() if k != "dbname"}
    return target


def supplied_settings(working, url):
    parsed = dj_database_url.parse(url)
    if not parsed.get("NAME"):
        raise Refused("The scratch database URL names no database. Nothing was changed.")
    target = copy.deepcopy(working)
    for key in ("NAME", "USER", "PASSWORD", "HOST", "PORT"):
        target[key] = parsed.get(key) or ""
    target["OPTIONS"] = dict(parsed.get("OPTIONS") or {})
    return target


@contextmanager
def opened(settings_dict, purpose):
    """A connection made by Django's backend for `settings_dict`, in autocommit."""
    wrapper = connections["default"].__class__(
        copy.deepcopy(settings_dict), alias=f"acceptance-{purpose}"
    )
    try:
        wrapper.ensure_connection()
        yield wrapper
    finally:
        wrapper.close()


def read_identity(wrapper):
    with wrapper.cursor() as cursor:
        cursor.execute(IDENTITY)
        return tuple(str(v) for v in cursor.fetchone())


def check(identity, *, name=None, expected=None, working=None, server=None, where):
    """Raise GuardTripped unless `identity` is the expected database and not the working one.
    `server` is the identity of a database that must be on the same server (port, start time)."""
    if name is not None and identity[0] != name:
        raise GuardTripped(f"{where}: connected to database {identity[0]!r}, expected {name!r}.")
    if expected is not None and identity != expected:
        raise GuardTripped(f"{where}: the database is not the one this run checked before.")
    if working is not None and identity == working:
        raise GuardTripped(f"{where}: the connection reached the working database.")
    if server is not None and identity[2:] != server[2:]:
        raise GuardTripped(f"{where}: the connection reached another database server.")


@contextmanager
def checked(settings_dict, purpose, **expect):
    """An opened connection whose identity was just checked on that same connection."""
    with opened(settings_dict, purpose) as wrapper:
        identity = read_identity(wrapper)
        check(identity, where=purpose, **expect)
        wrapper.acceptance_identity = identity
        yield wrapper


def run_sql(wrapper, statement, params=None):
    with wrapper.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.fetchall() if cursor.description else None


def lock_key(identity_name, scratch):
    digest = hashlib.sha256(f"smart-roots-acceptance:{identity_name}:{scratch}".encode())
    return int.from_bytes(digest.digest()[:8], "big", signed=True)


def describe(wrapper):
    """('empty' | 'acceptance' | 'other', detail) for the database `wrapper` is connected to."""
    relations = run_sql(
        wrapper,
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "AND n.nspname NOT LIKE 'pg\\_toast%%' AND n.nspname NOT LIKE 'pg\\_temp%%' "
        "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')",
    )[0][0]
    if not relations:
        return "empty", "no tables"
    marked = run_sql(
        wrapper,
        "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = %s AND c.relkind = 'r')",
        [MARKER],
    )[0][0]
    if not marked:
        return "other", f"{relations} tables or sequences and no acceptance-run marker"
    has_lab = run_sql(
        wrapper,
        "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = 'core_lab')",
    )[0][0]
    if has_lab:
        from .journey import LAB_NAME

        names = [r[0] for r in run_sql(wrapper, "SELECT name FROM core_lab")]
        if names and names != [LAB_NAME]:
            return "other", "it carries the marker but holds another laboratory's records"
    return "acceptance", "left by an earlier acceptance run"


def owns_schema(wrapper):
    return run_sql(
        wrapper,
        "SELECT pg_has_role(current_user, nspowner, 'MEMBER') FROM pg_namespace "
        "WHERE nspname = current_schema()",
    )[0][0]


def mark(wrapper):
    quote = wrapper.ops.quote_name
    run_sql(
        wrapper,
        f"CREATE TABLE {quote(MARKER)} (created_at timestamptz NOT NULL DEFAULT now(), note text)",
    )
    run_sql(wrapper, f"INSERT INTO {quote(MARKER)} (note) VALUES (%s)", [MARKER_NOTE])


def empty(wrapper):
    """Drop and recreate the connection's current schema (the role must own it)."""
    schema = run_sql(wrapper, "SELECT current_schema()")[0][0]
    quote = wrapper.ops.quote_name
    run_sql(wrapper, f"DROP SCHEMA {quote(schema)} CASCADE")
    run_sql(wrapper, f"CREATE SCHEMA {quote(schema)}")


def database_oid(server, name):
    rows = run_sql(server, "SELECT oid::text FROM pg_database WHERE datname = %s", [name])
    return rows[0][0] if rows else None


def migration_state():
    """(applied migrations as 'app.name', pending count, unknown applied count) for the
    database Django is connected to; read only."""
    executor = MigrationExecutor(connection)
    applied = sorted(
        f"{app}.{name}" for app, name in MigrationRecorder(connection).applied_migrations()
    )
    pending = len(executor.migration_plan(executor.loader.graph.leaf_nodes()))
    unknown = len(
        executor.loader.applied_migrations.keys() - executor.loader.disk_migrations.keys()
    )
    return applied, pending, unknown


def switch(settings_dict):
    """Point Django's own connection at `settings_dict`."""
    connection.close()
    for key in ("NAME", "USER", "PASSWORD", "HOST", "PORT", "OPTIONS"):
        connection.settings_dict[key] = copy.deepcopy(settings_dict.get(key))


# A shorter database password, host or user name occurs in ordinary text ("admin", a year):
# replacing it would garble the record and show a reader where the value was. Connection strings
# are removed by pattern at any length and database errors are recorded without their message,
# so these literal replacements are a second line only.
MIN_LITERAL = 8


def long_enough(values):
    return [str(v) for v in values if v and len(str(v)) >= MIN_LITERAL]


def secrets_of(settings_dict):
    """(literal secrets, names) to keep out of the evidence for a settings dict."""
    return (
        long_enough([settings_dict.get("PASSWORD")]),
        long_enough([settings_dict.get("HOST"), settings_dict.get("USER")]),
    )


def working_identity(working):
    try:
        with opened(working, "working") as wrapper:
            return read_identity(wrapper)
    except DatabaseError as exc:
        raise Refused(
            "Could not read the working database's identity, so the run cannot prove the scratch "
            f"database is a different one ({describe_db_error(exc)}). Nothing was changed."
        ) from None


@contextmanager
def scratch_database(
    *, url=None, keep=False, drop_stale=False, say=lambda text: None, secrets=None
):
    """Point Django at a migrated scratch database for the duration of the block.

    Yields a dict describing the database for the evidence (never credentials or host names).
    After the block, `info["cleanup"]` says what happened to the scratch database; a cleanup
    error is recorded there, not raised.
    """
    working = copy.deepcopy(dict(connection.settings_dict))
    info = {"kept": keep, "cleanup": None}
    secrets = secrets if secrets is not None else ([], [])
    for found, into in zip(secrets_of(working), secrets, strict=True):
        into.extend(found)
    try:
        _, info["working_pending"], info["working_unknown"] = migration_state()
        info["working_checked"] = True
    except DatabaseError:  # an unmigrated working database is fine; its identity is not optional
        info["working_checked"] = False
    working_id = working_identity(working)
    if url:
        target = supplied_settings(working, url)
        for found, into in zip(secrets_of(target), secrets, strict=True):
            into.extend(found)
        info.update(mode="supplied", name=target["NAME"])
        cm = _supplied(working, working_id, target, say)
    else:
        name = scratch_name(working["NAME"])
        target = target_settings(working, name)
        info.update(mode="created", name=name)
        cm = _created(working, working_id, target, drop_stale, say)
    with cm as (lock_holder, scratch_id, cleanup):
        try:
            switch(target)
            with connection.cursor() as cursor:
                cursor.execute(IDENTITY)
                current = tuple(str(v) for v in cursor.fetchone())
            check(current, expected=scratch_id, working=working_id, where="Django connection")
            say(f"Migrating the scratch database {info['name']} from empty.")
            call_command("migrate", interactive=False, verbosity=0)
            with connection.cursor() as cursor:
                cursor.execute("SHOW server_version")
                info["postgresql"] = cursor.fetchone()[0]
            info["applied_migrations"], _, _ = migration_state()
            yield info
        except GuardTripped as exc:
            switch(working)
            raise GuardTripped(
                f"{exc} Stopped without cleaning up: the scratch database {info['name']} exists "
                "and was left as it is; inspect it before removing it."
            ) from None
        except BaseException:
            switch(working)
            info["cleanup"] = _cleanup(keep, cleanup, info, say)
            raise
        switch(working)
        info["cleanup"] = _cleanup(keep, cleanup, info, say)


def _cleanup(keep, cleanup, info, say):
    if keep:
        say(f"Scratch database {info['name']} kept for inspection.")
        return "kept for inspection"
    try:
        result = cleanup()
    except GuardTripped as exc:  # no cleanup at all; recorded, and the package is still written
        message = f"not done: {exc} The database was left as it is"
        say(f"Scratch database {info['name']}: cleanup {message}.")
        return message
    except Exception as exc:  # recorded in the evidence; the package is still written
        kind = describe_db_error(exc) if isinstance(exc, DatabaseError) else type(exc).__name__
        message = f"failed ({kind}); remove it yourself"
        say(f"Scratch database {info['name']}: cleanup {message}.")
        return message
    say(f"Scratch database {info['name']} {result}.")
    return result


@contextmanager
def _lock(holder, key, name):
    acquired = run_sql(holder, "SELECT pg_try_advisory_lock(%s)", [key])[0][0]
    if not acquired:
        raise Refused(
            f"Another acceptance run is using the scratch database {name}. Wait for it to "
            "finish. Nothing was changed."
        )
    yield


@contextmanager
def _created(working, working_id, target, drop_stale, say):
    name = target["NAME"]
    if name == working["NAME"]:
        raise Refused("The scratch database name would equal the working database name.")
    with checked(working, "server", expected=working_id) as server:
        with _lock(server, lock_key(working_id[0], name), name):
            check(read_identity(server), expected=working_id, where="server")
            if database_oid(server, name) is not None:
                if not drop_stale:
                    raise Refused(
                        f"A scratch database named {name} exists, left by an earlier run that "
                        "was stopped or kept. Inspect it, then remove it by running again with "
                        "--drop-stale-scratch-database. Nothing was changed."
                    )
                with checked(
                    target, "stale", name=name, working=working_id, server=working_id
                ) as stale:
                    state, detail = describe(stale)
                    stale_oid = stale.acceptance_identity[1]
                if state == "other":
                    raise Refused(f"Refusing to drop {name}: {detail}. Nothing was changed.")
                check(read_identity(server), expected=working_id, where="server")
                if database_oid(server, name) != stale_oid:
                    raise GuardTripped(f"{name} changed while it was being checked.")
                say(f"Dropping the scratch database {name} left by an earlier run.")
                _drop(server, name)
            check(read_identity(server), expected=working_id, where="server")
            try:
                run_sql(server, f"CREATE DATABASE {server.ops.quote_name(name)} TEMPLATE template0")
            except DatabaseError as exc:
                if sqlstate(exc) == "42501":
                    raise Refused(
                        "The database role cannot create databases. Create an empty database "
                        "for acceptance runs and give its URL in ACCEPTANCE_DATABASE_URL (or "
                        "--scratch-database-url). Nothing was changed."
                    ) from None
                raise Refused(
                    f"Could not create the scratch database {name} ({describe_db_error(exc)}). "
                    "Nothing was changed."
                ) from None
            try:
                with checked(
                    target, "scratch", name=name, working=working_id, server=working_id
                ) as scratch:
                    scratch_id = scratch.acceptance_identity
                    if scratch_id[1] != database_oid(server, name):
                        raise GuardTripped(f"scratch: {name} is not the database just created.")
                    mark(scratch)
            except GuardTripped as exc:
                raise GuardTripped(
                    f"{exc} The scratch database {name} was created on the working database's "
                    "server and left as it is; inspect it before removing it."
                ) from None

            def cleanup():
                check(read_identity(server), expected=working_id, where="server")
                if database_oid(server, name) != scratch_id[1]:
                    raise GuardTripped(f"{name} is not the database this run created.")
                _drop(server, name)
                return "dropped"

            yield server, scratch_id, cleanup


def _drop(server, name):
    statement = f"DROP DATABASE {server.ops.quote_name(name)}"
    try:
        run_sql(server, statement)
    except DatabaseError as exc:
        if sqlstate(exc) != "55006":  # object_in_use: a lingering session
            raise
        run_sql(server, statement + " WITH (FORCE)")


@contextmanager
def _supplied(working, working_id, target, say):
    same_text = [str(target.get(k) or "") for k in ("NAME", "HOST", "PORT")] == [
        str(working.get(k) or "") for k in ("NAME", "HOST", "PORT")
    ]
    if same_text:
        raise Refused(
            "Refusing to run: the scratch database URL points at the configured working "
            "database. Acceptance runs never use the laboratory's database. Nothing was changed."
        )
    try:  # first contact: a mismatch here means nothing was touched yet
        with checked(target, "probe", name=target["NAME"], working=working_id):
            pass
    except GuardTripped:
        raise GuardTripped(
            "Refusing to run: the scratch database URL reaches the configured working database, "
            "or not the database it names. Acceptance runs never use the laboratory's database. "
            "Nothing was changed."
        ) from None
    except DatabaseError as exc:
        raise Refused(
            f"Could not connect to the scratch database ({describe_db_error(exc)}). Nothing was "
            "changed."
        ) from None
    with checked(target, "scratch", name=target["NAME"], working=working_id) as scratch:
        with _lock(
            scratch, lock_key(scratch.acceptance_identity[0], target["NAME"]), target["NAME"]
        ):
            state, detail = describe(scratch)
            if state == "other":
                raise Refused(
                    f"Refusing to use the scratch database: it is not empty ({detail}). Give "
                    "the URL of an empty database created for acceptance runs. Nothing was "
                    "changed."
                )
            if not owns_schema(scratch):
                raise Refused(
                    "Refusing to use the scratch database: this role does not own its "
                    "current schema, so the run could not empty it afterwards. Create the "
                    "database owned by this role (or give the role ownership of the schema). "
                    "Nothing was changed."
                )
            scratch_id = scratch.acceptance_identity
            check(read_identity(scratch), expected=scratch_id, working=working_id, where="scratch")
            if state == "acceptance":
                say("The supplied scratch database holds an earlier acceptance run; emptying it.")
                empty(scratch)
            mark(scratch)

            def cleanup():
                with checked(
                    target, "scratch-cleanup", expected=scratch_id, working=working_id
                ) as again:
                    if describe(again)[0] != "acceptance":
                        raise GuardTripped("The scratch database no longer carries the marker.")
                    empty(again)
                return "emptied"

            yield scratch, scratch_id, cleanup
