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
results, MDL records, uploaded laboratory logos) raise an error on `UPDATE` or `DELETE`. They
do not block `INSERT`, so `pg_restore` works. They do not block `TRUNCATE`, `DROP TABLE` or
schema changes, and the table owner or a superuser can disable them
(`ALTER TABLE ... DISABLE TRIGGER`, `session_replication_role`). Runtime and migration use
the same database role today. Do not repair history in place; restore into a new database
and compare instead. This is not tamper evidence against database administrators.

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

One case to check: holding-time rules that name an analyte (migration `analytical.0009`). A
release from before that change reads every active rule as method-wide, so it holds each
analyte of the method to the shortest of those limits (stricter, never a silent pass), and its
rule form, import and seed can refuse or pick the wrong row. Create analyte rules only once no
instance of an older release is running, and deactivate them before running an older release
against the database. The migration's reverse refuses while any rule names an analyte.

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

## Acceptance run after installation and upgrade

`run_acceptance` runs scripted checks of the first journey and the controls it rests on (role
refusals, client isolation, independent review, release permission, holding time, QC gates,
report immutability and amendments, audit events) on synthetic records, and writes an evidence
package for the laboratory to keep. Run it after installing, after each upgrade once the
release's migrations have run, and after changing the database server or container image.

```sh
python manage.py run_acceptance --operator "Full Name" --output /path/to/new-directory
```

It never uses the laboratory's records. It creates a database named
`acceptance_<database name>` on the same server, migrates it from empty, runs the steps and
drops it; this takes well under a minute. The database role needs `CREATEDB`. Where it has not,
create an empty database for acceptance runs, owned by the application's role (the role must
own the database's current schema so the run can empty it), and pass its URL in
`ACCEPTANCE_DATABASE_URL` (or `--scratch-database-url`, which shows the password in the process
list). The run refuses a URL that reaches the working database, however it is written, and a
database that holds anything other than an earlier acceptance run, and empties it afterwards.
On the working database the run only reads the migration table and the server's identity and,
in the default mode, holds an advisory lock and creates and drops the scratch database; a
second run started meanwhile is refused.

Every connection that creates, marks, empties or drops checks first that it reached the
database it should and not the working one. If such a check fails, the run stops and cleans up
nothing; the message names what exists. Ctrl-C ends the run and removes the scratch database.
A killed process leaves `acceptance_<database name>` behind (marked by an
`acceptance_scratch_marker` table); the next run reports it and stops until you pass
`--drop-stale-scratch-database`. `--keep-scratch-database` leaves it for inspection; it then
holds the synthetic accounts' password hashes, so drop it when done.

The run uses the deployment's own settings. Run it with `DEBUG=false` and the production
settings; with `DEBUG=true` the report says the production protections were off. Requests carry
what the configured proxy settings expect: the `X-Forwarded-Proto` header of
`SECURE_PROXY_SSL_HEADER`, and the client-address header named by
`ALLAUTH_TRUSTED_CLIENT_IP_HEADER` or an `X-Forwarded-For` with `ALLAUTH_TRUSTED_PROXY_COUNT`
entries, each with `127.0.0.1`. The run signs in fewer than fifteen times, within the sign-in
rate limits. Inside the container, write to a mounted directory the image's user (uid 10001)
can write, or the package is lost when the container exits; on a hosted service, copy the
package off the instance before the shell closes.

The package, in a new or empty directory (default: `smart-roots-acceptance-<UTC time>` in the
current directory, never inside the static or website directories). The run creates the
directory and tests that it can write there before anything else:

| File | Contents |
| --- | --- |
| `report.html`, `report.pdf` | Software version and commit (where the environment or a Git checkout supplies it), Python, Django and PostgreSQL versions, time zone and security settings, migrations, the operator as stated, start and finish times, every step with requirement, expected and observed result, and blank Performed by and Reviewed by lines |
| `evidence.json` | The same, with every observation and the full migration list |
| `artifacts/` | The issued and amended report PDFs the run produced |
| `manifest-sha256.txt` | SHA-256 of every other file; check with `sha256sum -c manifest-sha256.txt` |

| Exit status | Meaning |
| --- | --- |
| 0 | Every step passed and the package is complete |
| 1 | The package was written, but a step failed or did not run, `report.pdf` could not be rendered, or the scratch database could not be removed (the report says which) |
| 2 | Refused or stopped before any step ran; the message says whether anything was created |
| 3 | Unexpected failure. No package was written, or files without a manifest were left because the package could not be completed; the message says which |

Keep the package with the laboratory's other records of the installation; sign the printed
report as your procedure requires.

What it does not show: anything about the laboratory's own methods, limits, catalog, accounts
or records (it uses the synthetic `DEMO-` method set), backups and restore, mail delivery,
browser behaviour or performance. Requests are made in process, so TLS, the proxy and Gunicorn
are not exercised; check `/ready/` over the real address separately. A passing run is a record
that these checks passed on this installation at that time, not a validation of the software
or the laboratory; the laboratory decides what it means in its quality system.

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

Some platforms instead put the visitor's address in a header of their own and replace whatever a
client sends in it. If yours documents such a header, set `ALLAUTH_TRUSTED_CLIENT_IP_HEADER` to
its name; it is then used instead of the proxy count. Use it only for a header the platform
guarantees: a header that a client can set lets anyone choose the address they are counted under.

Limit counters are kept in each worker process's memory. With several Gunicorn workers or
instances the effective limit is multiplied accordingly; configure a shared Django cache if you
need exact limits.

## Browser security headers

Every response carries a Content Security Policy that allows scripts, styles, images, fonts and
connections from the deployment's own origin only, with no inline scripts or styles, and a
Permissions-Policy that turns off camera, microphone, geolocation, payment and USB access. The
application's own pages need nothing more. If a deployment adds approved sources of its own, set
`CONTENT_SECURITY_POLICY` to the complete replacement policy; it is sent as given, so keep
`frame-ancestors 'none'` and avoid `'unsafe-inline'` and `'unsafe-eval'`.

## Request size

The application refuses a request that declares a body larger than `MAX_REQUEST_BODY_BYTES`
(12 MiB by default; the largest upload a form accepts is 10 MiB) before reading it. This checks
the declared length only. Keep a body limit on the proxy or load balancer in front of the
application as well, because a request without a declared length is not covered here.

## HTTPS policy

With `DEBUG=false` the application redirects HTTP to HTTPS, marks its cookies secure and sends
`Strict-Transport-Security` for one hour. The short default limits the damage of a mistake.
Once every host that serves the deployment works over HTTPS only, raise
`SECURE_HSTS_SECONDS` (one year is `31536000`). Set `SECURE_HSTS_INCLUDE_SUBDOMAINS=true` only if
every subdomain of the host is served over HTTPS as well; browsers remember the policy for the
whole period and it cannot be withdrawn early.

## Logs

Each request writes one line to standard output (retained by the platform) from
`core/request_logging.py`: request ID, method, matched URL route pattern, status and duration.
The raw path is never logged because URL segments carry reset and confirmation tokens and record
identifiers; a request shows `route=accounts/confirm-email/<key>/`, or `<unmatched>` for paths
that match nothing. The ID is generated by the server, never taken from a client header, and is
returned in the `X-Request-ID` response header. Quote it to find related errors.

Sign-in activity writes `roots.signin` lines to the same stdout stream: `signin.success`,
`signin.failed` and `signout`, with the stored username. Unknown or ambiguous identifiers show
`user=<unknown>`; submitted login text, passwords and codes are never logged. Usernames are
URL-escaped so they cannot add log lines or fields. Password failures include attempts refused
by the sign-in rate limit; rejected email sign-in codes for known accounts and rejected MFA
sign-in codes also log a failure. Success is logged once, after all required authentication
stages complete. Explicit account and administration sign-outs are logged; anonymous sign-outs,
abandoned authentication stages and browser/session expiry do not emit a sign-out event.
These are operational logs, separate from laboratory audit history and database backups; a demo
database reset does not erase the platform's existing logs. Treat usernames as personal data and
set an appropriate retention period or stream to a controlled destination for longer history.

On Render, open the web service's **Logs** page and search for `roots.signin` (or an event such
as `signin.failed`). Every application log line also has `render_request_id`, the validated
`Rndr-Id` header, or `-` when absent or malformed. This field is for correlation only; it never
replaces the server-generated `request_id` or grants trust to the caller. Search that Render ID
to find the associated HTTP request log when the workspace has Pro or higher. Runtime log
retention follows the workspace's plan. Verify the header reaches the app
with a sign-in after deployment. Source: [Render logging](https://render.com/docs/logging),
checked 2026-11-12.

Django's own error and security records are rewritten to the same ID, status, route and exception
class. Tracebacks keep their frames but exception messages are withheld, because Django and
libraries can embed request details in them. Query strings, bodies, cookies, other headers and
local variables are not logged; usernames appear only in the sign-in activity lines described
above. Frame lines show source code text, never values.

Limit: Gunicorn's access log and the development server's own request line (both log the raw path)
are off, and Gunicorn's error log stays on for worker-level problems. Messages that application code logs itself are not rewritten, so
do not log submitted values. Credentials, `DATABASE_URL`, `SECRET_KEY` and SMTP passwords belong only
in environment settings or the provider's secret store.

## Public demonstration

A public demonstration lets anyone sign in with shared accounts, so it must return to a known
state by itself. This is for a deployment that holds only the seed's synthetic records. Do not
do any of it where a laboratory keeps its records.

`seed_demo --announce-accounts` publishes the shared accounts on the sign-in page and stops them
changing their own credentials (see the README). `reset_demo` then erases every record and runs
the seed again:

```sh
ALLOW_DEMO_RESET=true python manage.py reset_demo --full --announce-accounts
```

It asks for confirmation unless `--noinput` is given, and refuses to run unless all of these hold:

| Check | Why |
| --- | --- |
| `ALLOW_DEMO_RESET=true` in the job's environment | The deployment opts in; a laboratory's deployment never sets it |
| The database's laboratory is the seed's "Example Environmental Lab", or the database is empty | A database set up with `setup_lab` is never erased |
| No migration is pending and none is unknown to the release | A job from another release than the web service would seed the wrong schema |

The erase and the seed are one transaction. Requests that arrive meanwhile wait, usually well
under a minute, then see the fresh demonstration; everyone is signed out. If the seed fails, the
transaction rolls back and nothing changes. The erase empties every application table, including
accounts, sessions, custody and audit history and issued reports, and restarts identifiers such
as sample accession numbers. The database triggers that keep history rows from being updated or
deleted do not apply to this whole-table erase; that is deliberate and is the reason for the
checks above.

On Render the schedule is `render.demo.yaml`, a Blueprint separate from `render.yaml`. The deploy
button and a laboratory's Blueprint never create it. In the workspace that already runs the
demonstration from `render.yaml`:

1. Deploy the release to the web service first; its pre-deploy step migrates.
2. Create a new Blueprint from the same repository and branch with **Blueprint Path**
   `render.demo.yaml`. It adds one paid cron job, `smart-roots-lims-demo-reset`, and refers to the
   existing web service and database by name. Render asks once for `DEMO_PASSWORD`, which will be
   public, and `DEMO_ADMIN_PASSWORD`, which must differ and stay private.
3. Use **Trigger Run** on the job to reset straight away, or wait for the schedule (daily at
   08:00 UTC). Change the time in the file's `schedule` and `DEMO_RESET_NOTE` together.

Both files set auto-deploy off, the right default for a laboratory: deploy the job and the web
service together for every release, or the job refuses to run until they match.

The public demonstration instead shows the current `develop` branch. Its web service and reset job
both track `develop` with Render's **After CI Checks Pass** auto-deploy (`checksPass`), set on the
two services (2026-11-12) rather than in the files. Render waits for the `checks` workflow, which
cancels runs superseded by a newer push, so several pushes in a row end in one deploy of the last
commit and a commit that fails its checks is not deployed. Keep branch and trigger equal on both
services. Syncing the demonstration's Blueprint from `render.yaml` would turn the web service's
auto-deploy off again; its automatic sync is off. A deploy migrates but does not reseed: changed
seed data appears at the next reset.

The job is billed for the time it runs, with a monthly minimum;
check [Render pricing](https://render.com/pricing).

Sources, checked 2026-11-12: a Blueprint file may sit at a custom path and one repository may back
several independent Blueprints ([Render Blueprints](https://render.com/docs/infrastructure-as-code));
cron schedules use UTC, at most one run is active at a time and billing is by running time with a
monthly minimum ([cron jobs](https://render.com/docs/cronjobs)); a Blueprint may reference a
service that exists in the workspace but is not in the file
([Blueprint reference](https://render.com/docs/blueprint-spec)); `checksPass` deploys a commit
only after its CI checks pass ([deploys](https://render.com/docs/deploys)). `render blueprints validate
render.demo.yaml` (CLI 2.36.0) accepted the file against the demonstration's workspace on that
date, planning one new service; it creates nothing.
