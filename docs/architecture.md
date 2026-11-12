# Architecture

Baseline: 2026-11-08. Product policy: `docs/product.md`; installed versions: manifests/lockfiles.
Confirmed: commercial US water/wastewater and soil lab, one lab organization per deployment.
Modest volume and online operation are assumptions. No shared tenancy or offline sync.

## Stack

Use a Django monolith with server-rendered pages and PostgreSQL. Python is a good fit for
exact decimal data handling and laboratory integrations; Django supplies mature forms,
authentication, transactions, migrations and an ORM in one conventional application.

| Area | Selected baseline | Reason |
| --- | --- | --- |
| Runtime/framework | Python 3.14.10; Django 5.2.19 LTS | Supported combination; Django security support through April 2028 |
| Database | PostgreSQL 17; Psycopg 3.3.8 | Exact numerics, constraints, row locks and managed backups |
| Identity | Django sessions/auth/groups + django-allauth 65.21.1 | Standard account lifecycle, rate limits and MFA capability |
| UI | Django templates/forms, Bootstrap 5.3.8, HTMX 2.0.11, DataTables 3.1.5 | Mature components and progressive enhancement without a separate SPA |
| Serving | Gunicorn 26.2.0, WhiteNoise 6.12.0 | One web service; locally served versioned static assets |
| Documents | WeasyPrint 70.0; python-barcode 0.16.1 | Reports and identity-label PDFs; standard Code 128 SVG generation |
| Statistics | SciPy 1.18.1 / NumPy | Standard distributions; retain exact source observations and validate numerical conversion boundaries |
| Tooling | uv lockfile; Ruff; pytest-django; Playwright | Reproducible environment and focused server/browser verification |

Versions were checked against publisher documentation and package registries on the baseline
date; `uv.lock` pins the resolved transitive dependencies and CI builds and checks the container.
Use current compatible security patches. PostgreSQL's managed minor patch is provider-controlled.
See [Django support](https://www.djangoproject.com/download/),
[Python compatibility](https://docs.djangoproject.com/en/5.2/faq/install/),
[allauth requirements](https://docs.allauth.org/en/latest/installation/requirements.html),
[Bootstrap](https://getbootstrap.com/docs/5.3/getting-started/introduction/),
[HTMX](https://htmx.org/docs/) and [WeasyPrint dependencies](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html).

| Credible alternative | Strength | Why not selected |
| --- | --- | --- |
| Rails 8.1 + Hotwire + PostgreSQL | Equally conventional full-stack app, strong CRUD conventions | Django's explicit forms/auth and Python integration ecosystem fit this project better; Rails remains viable |
| ASP.NET Core 10 LTS + Razor Pages + EF Core/Npgsql | Strong typing, mature identity/ORM, long support window | More framework surface for a small greenfield project; no existing .NET integration constraint |

This is a judgment about this project, not a framework benchmark. Evidence:
[Rails maintenance](https://guides.rubyonrails.org/maintenance_policy.html),
[.NET support](https://dotnet.microsoft.com/en-us/platform/support/policy/dotnet-core).
A React/API split adds contracts and deployment/testing surface without a demonstrated need.
Use DataTables with its Bootstrap adapter and ColReorder for column presentation only. Keep
Django-rendered rows, full-queryset filtering/sort links and pagination; disable client sorting,
search and paging. Browser preferences store column IDs/order/visibility only, with keyboard
controls and reset. No Ajax row API, server preference model, or second table/query framework.

## Application boundaries and integrity

Keep a few Django apps for accounts, receipt/custody, analytical work and reporting, within
one project and database. Views/forms handle transport and validation; small explicit domain
functions handle multi-record transitions in `transaction.atomic`. Ordinary ORM queries need
no repository wrapper. Use constraints and selective row locks/version checks for concurrency.
Do not encode release policy in signals or spread it across UI handlers.

Client pre-login records planned work; acceptance may create an empty order, never fabricated
receipt/custody. Delivery targets are explicit, audited order commitments and remain separate
from analytical holding times. Printable labels identify records without changing their state.
External results keep immutable source provenance and independent source acceptance; never
manufacture internal batches/QC. Physical disposal preserves electronic history and holds.

Use Django's user model extensibility from the first migration. Staff permissions use groups
and explicit capabilities. Client access additionally scopes every query/download to authorized
client records; one lab deployment still has multiple mutually isolated clients. Account
administration and explicit release permission are distinct; a reviewer may also authorize release.
Invitation-only access is the initial assumption; never commit bootstrap credentials. Allauth supplies login,
reset, verification and MFA mechanisms; lab roles and record authorization remain application
policy. Use CSRF protection, secure sessions, HTTPS and explicit trusted hosts/origins.

Store Decimal values with declared scale/precision, original supplied text, units, qualifiers,
limits and method version as distinct data. Precision and rounding rules need product policy.
Do not infer nondetects from zero or overwrite observations; preservation limits are per method.
Custody and result corrections retain actor, timestamp, reason and prior linkage. Mutations
write an attributed audit event in the same transaction; background/system actions have an
explicit identity. Prevent update/delete of audit and released records by the runtime DB role
where practical. Separate migration credentials. Application append-only records alone do not
prove resistance to database administrators; stronger tamper evidence is a future policy choice.

Release checks operate on a consistent revision of results, required QC and permissions.
Use deployment `TIME_ZONE` for new forms/CSV and report snapshots; `Lab.timezone` is a recorded
setup copy only. Freeze the chosen zone/offsets in issued snapshots; never reinterpret old records.
Capture an immutable report snapshot and render its PDF; before committing release, lock and
recheck that the underlying revision is unchanged. Store snapshot, PDF bytes/hash, version,
actor and amendment linkage atomically. A render failure leaves no released report. Downloads
return stored bytes, never a regeneration from today's mutable records or template. Amendment
creates a new version and preserves the old one. No email delivery is part of a release transaction.

Initially store bounded PDFs/attachments in separate PostgreSQL binary columns with metadata
and checksums; list queries must not fetch their bytes. Provisional 10 MiB per-file limit and
bounded request/report sizes keep this practical for a small demo. This buys transactional
consistency and one backup set at the expense of DB storage, memory and backup growth. Revisit
before real instrument files or substantial volume; use Django storage + private S3-compatible
object storage then, with staged writes, reconciliation and coordinated restore. Never put
persistent files on Render's ephemeral filesystem. Product must confirm volumes and retention.

Report rendering accepts only application templates and escaped data, with local approved
assets; deny arbitrary network/file fetching. Serve uploaded documents as authorized downloads.
Start with bounded synchronous work and explicit retryable failures. Add a standard durable
queue only when measured job duration/concurrency requires it; do not build an ad hoc scheduler.

## User experience

Bootstrap supplies standard controls; a coherent theme and task-oriented shell must deliver a
professional operational workspace. Tables need readable density, aligned numbers/units, useful
columns, server-side sorting/filtering/pagination and clear identity/status. Forms need logical
groups, explicit units, inline errors, preserved input and keyboard focus. Verify realistic
receipt/batch/review screens and PDF previews visually. HTMX preserves feedback/focus and CSRF;
serve pinned assets locally. Django admin is restricted maintenance tooling only.

Page help is versioned plain-text content selected from the authorized response's resolved route,
including distinct form variants. It introduces no record API or database state. The browser keeps
per-account panel visibility and width preferences, as it does table presentation preferences.
Route-coverage checks require help for every HTML page; downloads and transitions are documented
on their invoking pages. The document-page skill defines the authoring and maintenance standard.

## Deployment and recovery

One Docker web service on Render plus managed PostgreSQL in the same US region is the initial
shape. Docker pins Python, PDF system libraries and fonts; Gunicorn serves the app, WhiteNoise
serves collected static assets. No Redis, worker, separate frontend host or persistent disk.
The public presentation at `/about/` ships with the same service. Only its static HTML and
assets live under the [dedicated WhiteNoise root](https://whitenoise.readthedocs.io/en/stable/django.html#whitenoise-root);
the workspace stays at `/` behind sign-in. The project also publishes those files, unchanged, to
GitHub Pages at <https://openbga.github.io/smart-roots-lims/>; its links are relative so both addresses work.
Build assets and collectstatic during image build; migrate once in a serialized pre-deploy step.
Use backward-compatible migrations while old/new instances overlap; destructive changes need
a staged migration and verified restore path. Rollback of code does not undo a database migration.

Health checks should distinguish process health from database readiness. Send structured logs
with request/record identifiers and errors, not scientific payloads or credentials. Configure
timeouts, connection limits and size limits against actual service capacity. Provide a deliberate
synthetic demo seed/reset operation; never run reset automatically on normal deployments.

Managed database recovery plus periodic encrypted off-provider exports are the minimum planned
backup approach. Prove restore to a separate database and compare custody/audit/report versions
before relying on it. Retention, RPO/RTO, region and the responsible operator remain decisions.
Report PDFs in the DB participate in the same recovery point. Real-data use needs an operational
runbook and tested recovery, not merely a successful demo deployment.

The Render Blueprint describes the web service, database, generated secret and required inputs.
The README button targets `https://render.com/deploy?repo=https://github.com/openbga/smart-roots-lims`.
A private fork is supported after granting the Render GitHub App access to it;
the deployer still reviews/approves resources. Set `autoDeployTrigger: off` for distributed button
instances. See [button requirements](https://render.com/docs/deploy-to-render).
CLI v2.36.0 documentation uses `render login`, workspace selection and
`render blueprints validate render.yaml`; platform validation needs authenticated workspace
context. Automation uses `RENDER_API_KEY`; validation does not create deployment resources.
See [CLI](https://render.com/docs/cli) and [Blueprint reference](https://render.com/docs/blueprint-spec).

The Blueprint creates paid web and PostgreSQL resources; check
[Render pricing](https://render.com/pricing) before deployment. Storage, outbound bandwidth,
build usage and any paid workspace are charged separately, and a deployment also needs an email
provider, off-provider backup storage and a domain.
PDF rendering may need the larger web tier; measure memory. Free Postgres expires after 30 days
and lacks managed recovery, so it is not a durable overnight/multi-day demo baseline.
Paid recovery windows are plan-dependent; [backup docs](https://render.com/docs/postgresql-backups)
specified 3 days on Hobby and 7 days on Pro+ when checked on 2026-11-08. Budget, credentials,
backup destination, demo access and reset policy are the deployer's decisions.
Deployers review and approve their own paid resources.

## Verification

Keep work in complete vertical slices: receipt/custody, analytical work, and reporting/client access.
Shared schema, migrations, routing and dependencies change in one place at a time.

The combined gate covers: locked install/lint, PostgreSQL tests for roles/client isolation,
custody/history, decimal results, QC/release concurrency and report amendments; migration from
empty and prior schema plus restore; and a browser journey from receipt through review/release
and authorized client download. Inspect professional layout, keyboard navigation and PDF page
output. Verify the container and Blueprint before deploying. Run the checks a change affects;
don't add tests that simply restate templates. Passing synthetic tests is not laboratory
validation. Commands are in [CONTRIBUTING](../CONTRIBUTING.md#verify-your-change).

## Open decisions

| Decision | Current assumption / who decides |
| --- | --- |
| Regulatory programs, methods, QC and release authority | The laboratory; no invented acceptance thresholds |
| Scale, uploads, retention and recovery objectives | Small bounded demo; the operator must confirm before real data |
| Hosting budget and demo access/reset policy | The deployer approves budget and defines access/reset policy |
| Email delivery, MFA enforcement and account recovery | Allauth mechanisms selected; operating policy and provider are the laboratory's choice |
| License | AGPL-3.0-only; canonical GNU LICENSE and dependency notices included; published 2026-11-11 |
| Instrument imports | Operator-mapped CSV/TSV; vendor validation awaits representative exports |
