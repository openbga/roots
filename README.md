# Smart Roots LIMS: free, open-source LIMS for environmental laboratories

[![checks](https://github.com/openbga/smart-roots-lims/actions/workflows/checks.yml/badge.svg)](https://github.com/openbga/smart-roots-lims/actions/workflows/checks.yml)

Smart Roots LIMS is an environmental laboratory LIMS for one commercial US water, wastewater and soil
lab per deployment, and an open-source project of BGASoft Inc. Built with Django, PostgreSQL and
Bootstrap/HTMX. AGPL-3.0-only. Source: <https://github.com/openbga/smart-roots-lims>.

Use synthetic data while release validation is in progress. This software supports laboratory
records; it does not certify compliance or accreditation.

## What it covers

- **Setup:** laboratory, staff roles, clients and projects, methods, analytes, tests, holding-time
  rules, QC requirements, limits and qualifiers.
- **Receipt and custody:** chains of custody with several samples, containers, receipt
  inspection, holds and dispositions, custody events and reasoned corrections.
- **Analytical work:** preparation and analytical batches, manual entry and mapped CSV/TSV
  imports, QC evaluation, independent review, control charts and MDL studies.
- **Reporting:** explicit release authorization, versioned immutable PDF reports, amendments and
  a client portal where each client sees only its own work.
- **Operations:** price lists and versioned quotes, client-submitted COCs, delivery scheduling,
  labels and subcontracted results.
- **Laboratory records:** materials, lots and stock movements, equipment, training,
  nonconformance, and physical sample retention and disposal.

Corrections add a record with a reason. Original observations, custody events and issued
reports are never overwritten. The NYSDEC EDD export is exploratory and requires recipient
validation; it is not an accepted regulatory submission.

[Workflow rules and limits](docs/workflows.md) describes each of these in more detail. The
[product presentation](https://openbga.github.io/smart-roots-lims/) is also served at `/about/` on each
deployment.

![Sample list with search, filters and receipt dispositions](docs/images/samples.png)

![Technical review of a batch beside its QC evidence](docs/images/review.png)

Screens show the synthetic demo data.

## Hosted demo

Open the [Smart Roots LIMS demo](https://smart-roots-lims.onrender.com/) on Render to explore the
application with an existing demo account. The [demo presentation](https://smart-roots-lims.onrender.com/about/)
is available without signing in. Use synthetic data only.

## Try it with Docker

Requires Docker with Compose. This builds the application image and runs it with a local
PostgreSQL database in development mode, reachable only from your machine. It is for evaluation,
not for real laboratory data.

```sh
git clone https://github.com/openbga/smart-roots-lims.git
cd smart-roots-lims
docker compose --profile app up --build -d
# Choose a private password (at least 12 characters) for the synthetic demo accounts.
read -s DEMO_PASSWORD; export DEMO_PASSWORD
# --full loads a synthetic working week; without it you get the accounts and one sample.
docker compose --profile app exec -e DEMO_PASSWORD web python manage.py seed_demo --full
```

Open <http://127.0.0.1:8000> and sign in as `demo_custodian`, `demo_project_manager`,
`demo_analyst`, `demo_reviewer`, `demo_approver`, `demo_admin` or `demo_client` with that password.
Stop it with `docker compose --profile app down`; add `--volumes` to delete the trial database.

## Local development

Requires Python 3.14, [uv](https://docs.astral.sh/uv/) and PostgreSQL 17. Docker Compose can run
the development database. Commands below use the Unix shell:

```sh
uv sync --frozen
docker compose up -d db
export DEBUG=true
export DATABASE_URL=postgresql://roots:roots@127.0.0.1:55432/roots
uv run python manage.py migrate
# Supply a private, unique demo password (at least 12 characters) in the environment.
read -s DEMO_PASSWORD; export DEMO_PASSWORD
uv run python manage.py seed_demo
uv run python manage.py runserver 127.0.0.1:8000
```

Open <http://127.0.0.1:8000>. Seed accounts are `demo_custodian`, `demo_project_manager`,
`demo_analyst`, `demo_reviewer`, `demo_approver`, `demo_admin`, and `demo_client`, using the supplied
password. The seed is explicit and idempotent: it never resets existing passwords or deletes
records. It refuses an existing non-demo lab. No seed/reset runs during normal deployment.
Add `--full` for a synthetic working week: two clients, about 50 samples from receipt to report,
a batch awaiting review, a QC failure that needs a disposition, control-chart history, an MDL
study, and draft, released and amended reports, with the quotes, submissions, inventory,
equipment and training records. It takes about 20 seconds and adds nothing when run again.
Release requires `accounts.authorize_release`, which may be granted to a reviewer; nobody may
review their own results. Administrator/superuser status alone does not grant release. A newly created
`demo_approver` receives this permission directly; the demo seed does not grant it to groups or
restore revoked permissions on rerun. Real-lab setup follows the explicit role policy below.

`.env.example` documents settings; Django does not automatically load `.env`. Never commit secrets.

Preview the product presentation alone with
`python3 -m http.server 8118 --bind 127.0.0.1 --directory website` at
<http://127.0.0.1:8118/about/>.

To check a change, see [Verify your change](CONTRIBUTING.md#verify-your-change).

## Set up your laboratory

On a fresh database, configure `DATABASE_URL`, `SECRET_KEY`, `TIME_ZONE` and the production host/
HTTPS settings from `.env.example`, then run:

```sh
uv run python manage.py migrate
uv run python manage.py setup_lab --lab-name "Your Lab Name" \
  --admin-username owner --admin-email owner@yourlab.example
```

The command prompts privately for the password, or accepts `SETUP_ADMIN_PASSWORD` from the
environment. It creates the laboratory, role groups and first administrator. Reruns retain
passwords, existing roles and revoked permissions. `--update-lab` explicitly renames the lab;
`--sync-timezone` records a changed deployment `TIME_ZONE`. Use the same deployment time zone
for new input and new reports; changing it does not reinterpret stored timestamps or issued reports.
Keep demo seeding separate from a real laboratory database.

Sign in at `/accounts/login/`. The owner manages users and group membership at `/admin/`.
Signed-in users may optionally enrol an authenticator app from the account menu's
"Two-factor authentication" page (`/accounts/2fa/`).
MFA is not enforced; the laboratory chooses its enforcement and account-recovery policy.
Assign operational roles to staff; create clients/projects before linking portal accounts.
For a portal user, select its client and leave staff/superuser flags, groups and permissions empty.
The first owner has the `admin` role; `createsuperuser` alone does not assign a laboratory role.

| Role | Work it enables |
| --- | --- |
| `admin` | Configure the lab and perform project-manager, custodian, analyst and reviewer work; independent review and all data-integrity checks still apply |
| `project_manager` | Maintain clients/projects, receive samples and prepare reports |
| `custodian` | Receive samples, record custody and resolve dispositions |
| `analyst` | Perform analytical work and enter results/QC |
| `reviewer` | Independently review results; never own work |
| `approver` | Bundle for explicit report-release permission; may also be assigned to a reviewer |

Operational admin access applies only to active internal accounts in the `admin` group. It does
not add group memberships or grant `accounts.authorize_release`. Admins cannot review their own
work; release still requires explicit permission. Client-linked accounts never gain internal access.

Before the first receipt:

1. In **Catalog**, create method versions and analytes, then tests with their matrix, unit and
   reporting limit, using your laboratory's approved configuration.
2. In **QC configuration**, set holding-time rules, QC requirements, limits and qualifiers for
   each method version. Creating a new method version does not copy this configuration.
3. Have a project manager create the client and project, then receive the COC and its samples.
   Assign independent review and explicit release authority to the appropriate staff first.

Account administration requires a superuser and is separate from laboratory roles. To add a role
to an existing owner from the service shell, use `setup_lab --admin-username owner --role admin`
(or another role); this adds membership without removing other roles or restoring permissions.

## Deploy to Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/openbga/smart-roots-lims)

The Blueprint packages a Docker web service and PostgreSQL 17. **It creates paid resources:**
review and accept the current charges in your Render account before deploying. For a private
fork, authorize Render's GitHub App to access the source. Distributed instances do not
automatically deploy upstream changes.

The Blueprint generates `SECRET_KEY` and wires `DATABASE_URL`. The app derives its allowed host
and trusted HTTPS origin from Render's `RENDER_EXTERNAL_HOSTNAME`; no manual origin input is
needed for the Render domain. Set comma-separated `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`
for additional custom domains/origins. HTTPS and secure cookies
are enabled when `DEBUG=false`. Set SMTP settings/`EMAIL_BACKEND` for real invitation/reset mail;
the packaged default disables mail delivery until configured. In the service shell, run
`python manage.py setup_lab --lab-name "Your Lab Name" --admin-username owner --admin-email owner@yourlab.example`
and follow the [laboratory setup steps](#set-up-your-laboratory). Never embed passwords in the image
or Blueprint.

Build collects static assets; pre-deploy runs migrations once. Health endpoints are `/health/`
(process) and `/ready/` (database). No user uploads should be written to the ephemeral filesystem.
Back up PostgreSQL, retain encrypted off-provider exports, and prove restoration before real use;
see [Operations](docs/operations.md). Use compatible staged migrations: redeploying old code
does not roll back a changed database.

Authenticated validation, without provisioning: `DO_NOT_TRACK=1 render blueprints validate
render.yaml` after `render login` and workspace selection.
See [Render's deploy button documentation](https://render.com/docs/deploy-to-render).

## Documentation

| Document | What it covers |
| --- | --- |
| [Workflow rules and limits](docs/workflows.md) | What each workflow does and the limits to know before relying on it |
| [Product scope](docs/product.md) | What the product must do, the domain rules behind it and their sources |
| [Architecture](docs/architecture.md) | Stack, integrity and authorization design, deployment shape |
| [Operations](docs/operations.md) | Backup, restore, recovery and logs for a self-hosted deployment |
| [UI standards](docs/ui-standards.md) | Layout, navigation, table and form conventions |
| [Third-party notices](THIRD_PARTY_NOTICES.md) | Licences of bundled and required components |

Inside the application, the documentation icon at the top right opens help for the current page.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the ground rules, setup and
checks, and the [Code of Conduct](CODE_OF_CONDUCT.md). Report security problems privately as
described in [SECURITY.md](SECURITY.md). [AGENTS.md](AGENTS.md) holds the working rules for AI
coding agents.

## Project and support

Smart Roots LIMS is an open-source project of [BGASoft](https://bgasoft.com), a laboratory software company, and part of [OpenBGA](https://github.com/openbga).

**Community use.** The software is free under the AGPL-3.0-only licence and is provided as is. The project does not include a support commitment: issues and pull requests are welcome and are answered on a best-effort basis.

**Enterprise support.** BGASoft offers paid support and services for organizations that run this software. Are you looking for support? Contact [bgasoft.com](https://bgasoft.com). Please do not use GitHub issues for support requests of that kind.

## Licence

Copyright (C) 2026 BGASoft Inc. and OpenBGA contributors.

Smart Roots LIMS is licensed under the GNU Affero General Public License version 3 only
(AGPL-3.0-only); see [LICENSE](LICENSE) and [NOTICE](NOTICE). If you run a modified version for
users over a network, AGPL section 13 requires offering them its source: set `SOURCE_CODE_URL` to
your published source so the "Source code" link in the application footer points to it.

OpenBGA, Smart Roots LIMS and their logos are trademarks of BGASoft Inc. The licence
does not grant permission to use them (AGPL-3.0 section 7(e)), except as needed to describe the
origin of the software.

Dependency licenses remain their respective owners' terms; preserve notices when distributing
(see [Third-party notices](THIRD_PARTY_NOTICES.md)).
