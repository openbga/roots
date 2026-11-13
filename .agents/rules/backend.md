---
paths:
  - "*/models.py"
  - "*/views*.py"
  - "*/*_views.py"
  - "*/services*.py"
  - "*/forms*.py"
  - "*/*_forms.py"
  - "*/selectors.py"
  - "*/lookups.py"
  - "*/migrations/**"
  - "core/**"
  - "config/**"
---

# Backend rules

The principles are in `AGENTS.md` (rules 2–4, 7, 9): exact results, authorization on every read
and write, attributed audits, no homemade layers. This file names the code that implements them.
Reuse it; don't write a second version.

- **Who may act.** Use `accounts/roles.py`: `require_role(user, *roles)` / `has_role` for staff
  roles, `require_staff`, and `require_release_authority` (the explicit `authorize_release`
  permission; superuser alone is not enough). App role sets live in `<app>/permissions.py`
  (`commercial/permissions.py`, `laboratory/permissions.py`). Check inside the service, not only in
  the view. A hidden button or admin access is never the gate.
- **Client ownership.** Portal views use `client_required` (`reporting/portal_views.py`,
  `commercial/permissions.py`) and build every queryset from `user.client_id`
  (`client_reports`, `client_samples`, …). Return 404 for another client's record, never a
  filtered-out 200. Staff accounts have `client_id is None`; client accounts never hold roles.
- **No self-review.** Review, approval and release refuse the person who entered or submitted the
  result (`actor.pk in (revision.entered_by_id, revision.submitted_by_id)` in
  `analytical/services.py`; `reporting.content.assemble(actor=…)` blockers for release).
- **Transition and audit commit together.** Decorate the service with `@transaction.atomic`,
  lock with `select_for_update()` (or the app's `lock_*` helpers), recheck the state, change it,
  then call `core.audit.record_event(actor=, action=, obj=, before=, after=, reason=)`, which
  raises outside a transaction. Use `core.audit.snapshot(obj)` for before/after, never on account
  or credential models.
- **Concurrent edits.** Configuration edit forms use `core/concurrency.py`: `protect_form(form,
  instance)` adds the original-state token, and `check_original_state(instance, expected)` rejects
  a stale save while keeping the input. Drafts with a counter use `expected_revision`
  (`reporting.services.update_report`). Recheck after locking and before writing.
- **Exact values.** Parse with `analytical/parsing.py` (`parse_decimal`, `parse_result_text`,
  `parse_timestamp` in the lab time zone). Store `DecimalField(**VALUE_FIELD)` next to the
  verbatim `supplied_text` and an explicit `unit` CharField. Format for display with
  `reporting.content.number_text`. Never `float()` or `round()` a measured value.
- **Remote select lookups.** Register in `core/lookups.py`:
  `register(Lookup("<name>", staff(lambda request: …), SEARCH_FIELDS, ordering))`, or
  `portal_client(...)` for portal lists. The form's `ModelChoiceField` queryset still validates.
- **Per-user presentation** is `core/preferences.py` (`grid:<grid>:v<n>`, `page:<view name>`,
  `nav.collapsed`). It is presentation only and never widens a queryset.
- **Dates and clocks.** `core.due.due_state` decides overdue and soon for dated obligations.
  `analytical/holding.py` (`evaluate_rule`, `evaluate_for_entry`, `running_clocks`) evaluates
  holding times. Don't reimplement either in a view or template.
- **Migrations.** Additive and backward compatible while old and new code overlap. Add nullable
  or defaulted fields and backfill in a separate step. A destructive change is staged across
  releases (`docs/architecture.md`). Never edit a merged migration, never rewrite issued
  snapshots, and keep `makemigrations --check` clean.
- **Structure.** Domain logic lives in the app's `services.py` or named domain modules
  (`receipt/services.py`, `analytical/holding.py`) and reads in `selectors.py`. No generic
  service or repository base classes, and no workflow DSL.
- **Tests** go in `<app>/tests/` (cross-cutting ones in `tests/`), using the app's `conftest.py`
  fixtures. Cover the allowed and the refused role, another client's user, the audit event and
  the stale-edit path. List pages keep a query budget with more rows than one page
  (`CaptureQueriesContext`, as in `reporting/tests/test_query_counts.py` and
  `tests/test_dashboard.py`).
