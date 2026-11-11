# Operations: backup, restore and recovery

Runbook for operators of a self-hosted deployment. It covers what to protect and how to prove
a restore. Settings and setup steps live in the [README](../README.md); design rationale in
[architecture](architecture.md). Commands use placeholder credentials; keep real ones in your
shell or secret manager, never in Git, tickets or logs.

## What is protected

All state is in PostgreSQL: results and revisions, custody and audit events, issued report PDFs
and snapshots, subcontract source PDFs and EDD exports are binary columns in the same database.
There is no file storage to back up separately. The container filesystem is disposable.

Treat every dump as sensitive. It holds lab records and account data, including each user's
authenticator-app (TOTP) secret and recovery codes: the installed django-allauth stores them
unencrypted unless the deployment configures its encryption hook, which this project does not.
Encrypt backups and restrict who can read them.

`SECRET_KEY` is not stored in the database; keep it in your secret manager. Changing it signs
users out and invalidates outstanding password-reset and email-confirmation links, sign-in codes
and CSRF tokens. It does not make MFA enrolments unusable, because allauth does not derive MFA
secrets from it. Keep the same key when recovering unless you intend those effects.

**Immutability protection, exactly.** Row triggers on the history tables (audit and custody
events, result revisions, issued reports and files, EDD exports, subcontract and external
results, MDL records) raise an error on `UPDATE` or `DELETE`. They do not block `INSERT`, so
`pg_restore` works. They do not block `TRUNCATE`, `DROP TABLE` or schema changes, and the table
owner or a superuser can disable them (`ALTER TABLE ... DISABLE TRIGGER`,
`session_replication_role`). Runtime and migration use the same database role today. Do not
repair history in place; restore into a new database and compare instead. This is not tamper
evidence against database administrators.

## Decisions the operator must make

The software does not choose these. Record them before real data.

| Decision | Notes |
| --- | --- |
| Recovery point and time objectives | Render point-in-time recovery covers 3 days on Hobby workspaces and 7 on Pro or higher; Free databases have none ([Render backups](https://render.com/docs/postgresql-backups), checked 2026-11-09). |
| Off-provider destination and encryption | Render keeps logical exports seven days only. Store your own copies encrypted (for example `age` or `gpg`) at a location you control. |
| Retention period and responsible person | Lab policy and regulation decide; the software infers none. |
| Restore-test cadence | Suggested minimum: before go-live and after any major version or migration change. |

## Take a backup

Use PostgreSQL client tools of the same major version as the server (17).
`SOURCE_URL` is the database's external connection string; the Render dashboard shows it, and
your IP must be on the database allow list (the Blueprint default is empty, so none).
If you cannot connect externally, use the dashboard **Recovery → Create export**
instead, which yields a `.dir.tar.gz` directory-format archive for `pg_restore`.

```sh
export SOURCE_URL='postgresql://USER:PASSWORD@HOST/roots'   # placeholder
pg_dump --format=directory --jobs=4 --no-owner --file=roots-$(date -u +%Y%m%dT%H%MZ).dump "$SOURCE_URL"
```

Encrypt and copy the result off-provider, then record its date and size. Also capture a
verification summary (see below) beside it so a later restore can be compared.

## Restore into an isolated database

Never restore over the live database. Create an empty, separate PostgreSQL 17 database (a
second Render database, or the local Compose database on port 55432) and an app environment
that points only at it.

```sh
export TARGET_URL='postgresql://roots:roots@127.0.0.1:55432/roots_restore_check'   # placeholder
psql 'postgresql://roots:roots@127.0.0.1:55432/postgres' -c 'CREATE DATABASE roots_restore_check'
pg_restore --exit-on-error --no-owner --no-privileges --jobs=4 --dbname="$TARGET_URL" roots-YYYYMMDDTHHMMZ.dump
```

- `pg_restore` targets an empty database; `--clean` drops objects first and is not needed here.
- `--single-transaction` gives all-or-nothing behaviour but cannot be combined with `--jobs`.
- Roles named in the dump may not exist on the target; `--no-owner` makes you the owner.
  The runtime role then needs ordinary table privileges on the restored objects.
- Source of semantics: [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html).

For a Render point-in-time recovery, the platform creates a **new** database instance; do
not delete the original until you have verified the new one.

## Verify the restore

Counts only compare meaningfully at a consistent boundary. `pg_dump` itself reads one
snapshot, but queries you run separately do not share it, and the live database keeps changing.
Either suspend the web service (writes quiet) while you take the dump and run the source
summary, or skip the source comparison and rely on the self-consistency checks below, which
need no live comparison. Run the restored-database checks with the app configured as in
production (`DEBUG=false`) and `DATABASE_URL` set to the restored database.

Source summary, taken with writes quiet; save the output beside the dump:

```sql
select 'audit', count(*), max(id) from core_auditevent
union all select 'custody', count(*), max(id) from receipt_custodyevent
union all select 'result_revisions', count(*), max(id) from analytical_resultrevision
union all select 'report_versions', count(*), max(id) from reporting_reportversion
union all select 'report_files', count(*), null from reporting_reportfile;
```

1. `python manage.py migrate --check` exits successfully (no unapplied migrations).
2. The same summary on the restored database equals the saved source summary.
3. Immutability triggers match the source (run on both and diff the output):
   `select tgrelid::regclass, tgname from pg_trigger where not tgisinternal and tgname like '%immutable%' order by 1, 2`
4. Stored files still match their recorded hashes; each query must return 0:

   ```sql
   select count(*) from reporting_reportversion v join reporting_reportfile f
     on f.version_id = v.id where encode(sha256(f.pdf), 'hex') <> v.pdf_sha256;
   select count(*) from reporting_eddexport where encode(sha256(data), 'hex') <> sha256;
   select count(*) from receipt_subcontractresult
     where encode(sha256(source_pdf), 'hex') <> source_sha256;
   ```
5. Start the app against the restored database, check `/ready/`, sign in as an administrator
   and download one issued report.
6. On a scratch copy only, an `UPDATE core_auditevent SET reason = reason` must fail with
   "Historical records cannot be updated or deleted".

Record the result (date, backup file, checks passed) in your own change log. A backup that
has not passed these checks is untested.

## Recover production

Pick the situation first; they are different procedures.

**Bad release, database healthy.** Redeploy the previous code if it is compatible with the
current schema. Project policy is backward-compatible migrations, so the previous release usually runs
against a newer schema, but check the migrations added since it (a dropped or renamed column
breaks it). Do not restore the
database: that would discard records entered since the backup.

**Data loss or corruption.** Recover into a new database; never overwrite or discard the
current one.

1. Stop writes (suspend the web service) and keep the damaged database untouched.
2. Restore a backup or point-in-time recovery into a new database and verify it as above.
3. Records entered after the backup exist only in the damaged database. Export or
   reconcile them with the lab before choosing which database becomes production.
4. Point `DATABASE_URL` at the chosen database and deploy; pre-deploy runs
   `migrate --noinput`, a no-op if the schema is current.
5. Keep the other database until the lab signs off.

A migration that must be reverted needs a deliberately written reverse or a staged fix, not a
blind restore. Take a backup before each deploy that adds migrations.

## Sign-in limits behind a proxy

Failed sign-ins, password resets and sign-in code requests are limited per account and per
client address. The address is the connecting peer unless `ALLAUTH_TRUSTED_PROXY_COUNT` says how
many trusted proxies append to `X-Forwarded-For`. Behind a hosting platform's proxy the peer is
the proxy for every visitor, so with the default of `0` all visitors share one per-address limit:
repeated failed sign-ins by anyone can block sign-in for everyone until the window passes.

Set `ALLAUTH_TRUSTED_PROXY_COUNT` to the number of proxies you control or trust between the
visitor and the application. Verify it before relying on it: sign in from a known address and
confirm that address is the entry that many places from the end of the `X-Forwarded-For` header
your service receives. A count that is too high lets a visitor forge the address and escape the
limit; if the header has fewer entries than the count, the request fails. The Blueprint does not
set a value because the platform's proxy chain must be confirmed on the running service.

Limit counters are kept in each worker process's memory. With several Gunicorn workers or
instances the effective limit is multiplied accordingly; configure a shared Django cache if you
need exact limits.

## Logs

Each request writes one line to standard output (retained by the platform) from
`core/request_logging.py`: request ID, method, matched URL route pattern, status and duration.
The raw path is never logged because URL segments carry reset and confirmation tokens and record
identifiers; a request shows `route=accounts/confirm-email/<key>/`, or `<unmatched>` for paths
that match nothing. The ID is generated by the server, never taken from a client header, and is
returned in the `X-Request-ID` response header. Quote it to find related errors.

Django's own error and security records are rewritten to the same ID, status, route and exception
class. Tracebacks keep their frames but exception messages are withheld, because Django and
libraries can embed request details in them. Query strings, bodies, cookies, headers, usernames
and local variables are not logged. Frame lines show source code text, never values.

Limit: Gunicorn's access log and the development server's own request line (both log the raw path)
are off, and Gunicorn's error log stays on for worker-level problems. Messages that application code logs itself are not rewritten, so
do not log submitted values. Credentials, `DATABASE_URL`, `SECRET_KEY` and SMTP passwords belong only
in environment settings or the provider's secret store.
