# Contributing to Smart Roots LIMS

Thank you for your interest. Smart Roots LIMS is a LIMS for commercial US water, wastewater and
soil laboratories. Bug reports, fixes, documentation and focused improvements are welcome and
are reviewed on a best-effort basis.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md) before taking part. Report security
problems privately as described in the [security policy](SECURITY.md), never in a public issue.

## Before you start

- **Search first.** Check open and closed issues and pull requests.
- **Discuss larger changes.** Open an issue before starting a new workflow, a schema change or a
  new dependency, so the design can be agreed before you invest time. Small fixes can go
  straight to a pull request.
- **Know the scope.** [Product scope](docs/product.md) says what the product must do and what it
  must not claim. [Architecture](docs/architecture.md) explains the design constraints.

## Ground rules

These protect the people who rely on the records. A change that breaks one will not be merged.

1. **Synthetic data only.** No real client, site, permit, sample or personal data in code,
   tests, fixtures, screenshots, issues or pull requests. Use `example.invalid` addresses and
   clearly invented names.
2. **No compliance claims.** The software supports a laboratory's records. It does not make a
   laboratory compliant, accredited or certified. Do not invent laboratory policy, acceptance
   limits or method requirements; cite a public primary source with its date for any domain rule.
3. **Exact results.** Scientific quantities use `Decimal` and PostgreSQL `numeric` with explicit
   units, qualifiers and limits. Never a binary float, never a silent conversion.
4. **History is append-only.** Original observations, custody events, method versions and issued
   report versions are never rewritten. Corrections add a record with a reason.
5. **Authorize on the server.** Every read and mutation checks the role and the record's owner,
   including client portal access. Nobody reviews their own results. A hidden button is not a
   control.
6. **Transitions are atomic.** A state change, its checks and its attributed audit event commit
   together, and concurrent changes are rechecked.
7. **Keep it conventional.** Django, PostgreSQL and the established libraries already in use. No
   new framework layers, queues or abstractions without a demonstrated need.
8. **Keep other products out.** No material about other LIMS or LIS products or vendors.
   Regulatory sources, standards and data formats that a feature implements are fine.

## Development setup

Follow [Local development](README.md#local-development) in the README. You need Python 3.14,
[uv](https://docs.astral.sh/uv/) and PostgreSQL 17 (Docker Compose can run the database).

## Making a change

- The repository has two long-lived branches. `develop` is where changes are integrated;
  `main` is the stable branch that a laboratory's deployment tracks, and maintainers update it
  from `develop`. The public demonstration follows `develop`: each commit there is deployed once
  its checks pass. Neither can be deleted or force-pushed, and `main` accepts only commits whose
  checks have already passed. Branch from `develop`, open your pull request against `develop`, and
  keep each pull request to one coherent change.
- Add or update tests for behaviour you change. Tests concentrate on roles and client
  boundaries, custody and history, exact results, QC and release gates, report amendments and
  concurrency. Do not add tests that only restate framework behaviour or templates.
- **Update the page help.** Every page has operator help under `docs/pages/`. When a page, form,
  field, action, permission, filter or workflow rule changes, update its help in the same pull
  request. The [`document-page` skill](.agents/skills/document-page/SKILL.md) describes the standard and
  `tests/test_page_docs.py` checks coverage.
- Follow the [UI standards](docs/ui-standards.md) for templates, navigation and tables.
- Migrations must stay backward compatible with the previous release. Never edit a migration
  that has already been merged.
- Match the existing style. Ruff formats and lints the code; there is no separate style guide.

## Verify your change

Use the local development environment (`DEBUG=true` and your local `DATABASE_URL`).

```sh
uv run ruff check .
uv run ruff format --check .
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py collectstatic --noinput
REQUIRE_WEASYPRINT=1 uv run pytest
```

Notes:

- Collect static assets before running tests that render templates, and again after changing
  vendored or application assets. Tests keep WhiteNoise's strict manifest storage, and a fresh
  checkout has no collected manifest.
- Tests need PostgreSQL. SQLite cannot verify the locking and database history protections. The
  configured database user must be allowed to create a test database.
- On macOS with Homebrew PDF libraries, run Python directly so the native library path is kept:
  `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib REQUIRE_WEASYPRINT=1 .venv/bin/python -m pytest`.
  Use the same environment with `.venv/bin/python manage.py runserver` for PDF previews.
- To run several test sessions at once against one PostgreSQL server, give each its own
  disposable database with `TEST_DATABASE_NAME=test_roots_<name>`. The name must start with
  `test_` and differ from the connection database. Never point it at a development or production
  database.

### Browser journeys

Browser tests are opt-in. They use a separate pytest PostgreSQL database, an ephemeral server
port and one headless Chromium worker, not your development server. The database is
`test_<database>_e2e`; when several people or agents run the suite at once, give each run its own
with `TEST_DATABASE_NAME=test_<you>` (the browser suite then uses `test_<you>_e2e`).

```sh
uv sync --frozen --group e2e
uv run --group e2e playwright install --only-shell chromium
uv run --group e2e python manage.py collectstatic --noinput
REQUIRE_WEASYPRINT=1 uv run --group e2e pytest -c e2e/pytest.ini e2e
```

PDF assertions need Poppler's `pdftotext` and `pdfinfo` (`brew install poppler` on macOS,
`apt-get install poppler-utils` on Debian or Ubuntu). On Linux add `--with-deps` to the browser
installation command. On Apple Silicon run the last command as
`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib REQUIRE_WEASYPRINT=1 .venv/bin/python -m pytest -c e2e/pytest.ini e2e`.

### Continuous integration

Every pull request runs the checks above, the browser journeys, and a container smoke test that
builds the Docker image, migrates a disposable PostgreSQL database, renders a PDF and starts
Gunicorn for health, readiness, sign-in and static checks. These checks never publish or deploy.
A separate workflow publishes the static presentation in `website/about` to GitHub Pages when it
changes on `main`.

## Pull requests

Describe what changed and why, how you verified it, and anything a reviewer should look at
closely. Say so if the change affects authorization, stored history, a migration or report
output. A maintainer reviews when time allows; please be patient and keep the discussion on the
pull request.

## Licence of contributions

Smart Roots LIMS is licensed under [AGPL-3.0-only](LICENSE). By submitting a contribution you agree
that it is licensed under the same terms, and you confirm that you have the right to submit it.
Do not copy code or text from sources whose licence is incompatible with AGPL-3.0-only, and
keep third-party notices intact (see [Third-party notices](THIRD_PARTY_NOTICES.md)).

## Working with coding agents

[`AGENTS.md`](AGENTS.md) holds the working rules for AI coding agents in this repository. They
are the same rules as above, in more detail. If you use an agent, you remain responsible for
the change you submit.

### Agent tooling

Shared agent instructions live in `.agents/`. `.claude/skills` and `.claude/rules` are symlinks
to `.agents/skills` and `.agents/rules`, so Claude Code and Codex (which reads `.agents/skills`)
load the same files.

| Kind | File | Use |
| --- | --- | --- |
| Skill | [`build-page`](.agents/skills/build-page/SKILL.md) | Building or changing any page, form, dialog or template: page types, shared components, controls, tokens, icons, lists |
| Skill | [`document-page`](.agents/skills/document-page/SKILL.md) | Writing the in-app page help under `docs/pages/` |
| Skill | [`project-code-review`](.agents/skills/project-code-review/SKILL.md) | The pre-PR gate: diff review against `AGENTS.md` and the UI rules, fixes, checks, browser journeys, before/after screenshots |
| Rule | [`frontend.md`](.agents/rules/frontend.md) | Short frontend rules: components, tokens, icons, controls, validation |

Before/after screenshots for pull requests come from `scripts/ui_compare/capture.py`
(see the `project-code-review` skill).
