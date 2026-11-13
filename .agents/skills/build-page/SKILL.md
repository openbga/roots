---
name: build-page
description: How to build or change any Smart Roots LIMS page, form, dialog or template the house way. Covers page types and the shared `{% load ui %}` components for each, form controls through the Django widgets (including the remote select with a registered lookup), tokens and Bootstrap Icons only, list columns and per-user preferences, panel spacing, and the must-see facts of a page. Use it whenever you add or edit a Django template, view context for a template, form or widget, list or grid, record page, dialog or app.css/JS used by pages, even for a "small" markup change. Then run document-page.
---

# Build or change a page

Pages here are assembled from shared parts, not written from scratch. Every concept already has
one component, so a page that copies markup drifts from the others and fails
`tests/test_ui_standards.py`. The standard is `docs/ui-standards.md`, and the short rule file
is `.agents/rules/frontend.md`. This skill is the working order. `references/components.md`
holds the tables (component registry, form controls, list column flags). Read it when you
reach the step that needs it.

## 1. Classify the page

Every page is one of the seven types in ui-standards §2. The type fixes the header, the layout,
the breadcrumbs and where the primary action goes:

| Type | Build it with | Primary action |
| --- | --- | --- |
| List | One `{% panel table=True hidden_title=True %}` with `{% grid_toolbar %}`, `{% sort_header %}` columns and `{% list_results %}` (table, pagination, empty, no-results and filter-error states) | `New <thing>` in `page_actions` |
| Record | `{% page_meta %}` (status first, at most four facts), `{% record_layout %}` (lead panel of `{% fact %}` rows in `dl.vr-facts`, an optional aside launch panel), related table panels, `{% history_panel %}` last | The next step for the record's state, in the header |
| Form | `{% form_page form=… cancel_url=… submit_label=… %}`, opened in the shared form dialog; the page is the no-JS fallback | The submit, in the action row |
| Action form | `{% form_page %}` with the reason field; title equals the button that opened it | The submit |
| Entry grid | A plain table of form fields (never DataTables), sticky `{% form_actions %}`, units in headers | Save |
| Overview | Panels of queue and count tables that link to filtered lists | None, or the one most frequent start |
| Document | Preview panels plus the decision in the header | The decision (Release version N …) |

Header (ui-standards §4): at most one primary and three secondary buttons. The rest go in
`{% more_actions %}`. Destructive actions come first, the primary last. A button-only state change is a
`{% post_button %}` in the header. Anything that needs input opens a form dialog.

## 2. Decide what must be seen first

Before writing markup, list the page's must-see facts for each role that opens it. These are the
things a person would act on wrongly if they missed them: status, holding time or due date
running out, on-hold or rejected disposition, returned results, QC exceptions, amendment state,
the record's identity. Then place them:

- Status and up to four identifying facts go in `{% page_meta %}`, never three panels down.
- Time pressure uses `{% due_pill %}` (dated obligations) or a warning or danger `{% status %}`
  (holding clocks at risk or expired) beside the date.
- A sample's matrix is `{% matrix_tag %}`. Blocking or attention states are a
  `{% notice %}` above the facts (`blocking=True` when it stops the task).
- Keep one primary emphasis per region, and never use colour as the only cue: every tone has text
  or an icon. Everything else stays quiet: muted text (`.vr-muted`), no extra pills.

Use only existing components for this. A new visual pattern is a design decision for the owner,
not something to add on the way.

## 3. Assemble it from components

- `{% load ui %}` and use the registered tags. Never `{% include %}` a registered component
  (the tag renders in an isolated context). Pass every value it needs as a parameter.
  `tests/test_include_isolation.py` fails if a component reads a page variable it wasn't given.
  When a component lacks a variation you need, add a parameter to the component (with its
  header comment and the `COMPONENTS` entry in `core/templatetags/ui.py`). Don't copy its
  markup. The registry is in `references/components.md`.
- Panels own their spacing. Every panel body insets content by `--vr-panel-inset` and separates
  stacked blocks by `--vr-stack`. Don't add padding or margin utilities inside a panel body to
  fix spacing. Use `{% panel_section %}` for sub-blocks, and `table=True` for a flush table card.
  Only spacing between panels (`class="mb-3"`) is set on the tag. No panel inside a panel.
- Lists: the server sorts, filters and paginates the whole authorized queryset (`core/grids.py`).
  DataTables only shows, hides and reorders columns. The identifier in the first column is the row
  link (`th[scope=row] > a.vr-row-link`). There's no Open or View link and no whole-row click. Give
  each column a stable `col` id, lock the identifier and actions, and mark secondary columns
  `priority="low"` so they hide below 1366px. Column layout, rows per page and sort are saved per
  user on the server (`core/preferences.py`, keys `grid:<grid>:v<n>` and `page:<view name>`). If
  you rename or remove a grid's column ids, bump the table's `data-vr-grid-version` so saved
  layouts in the old format are ignored.
- Wording follows ui-standards §9 (`New`, `Add`, `Record`, `Edit`, `Correct`, `Remove`), in
  sentence case. The title, breadcrumb, sidebar item and the button that opens a page use the same
  words.
- Roles and states (§10): a control a role can never use isn't rendered. One that's unavailable
  in the current state is disabled, with the reason as text beside it. Hiding a control is a
  courtesy. Authorize the read and the mutation in the view or service (AGENTS.md rule 4),
  including client ownership on portal pages.

## 4. Forms and controls

- Render fields through Django forms: `{% form_page %}`, `{{ form }}`, `{% field %}`. No
  hand-written `<input>`, `<select>`, `<datalist>`, or date, time, number or file input in a
  page template (`tests/test_controls.py`). Each control is a widget template in
  `templates/django/forms/widgets/` plus its initialiser in `static/js/controls.js`. See the
  controls table in `references/components.md`.
- A large or growing model choice (clients, projects, samples, staff, lots, equipment) uses
  `widget=core.lookups.RemoteSelect("<lookup>")` with a lookup registered in `core/lookups.py`.
  Its `queryset(request)` must apply the same authorization as the page (staff roles, or the
  portal user's own client) and raise `PermissionDenied` otherwise. The field's own queryset
  stays the validator. Add tests for staff versus client, inactive records, search and paging.
  Short configuration lists stay inline.
- Numbers are text inputs so the exact string reaches the `Decimal` field (AGENTS.md rule 3).
  Put units on the widget (`attrs={"unit": "mg/L"}`), not in the label.
- Validation is server-side. Errors render inline through `{% form_errors %}` and the field
  template, entered values are kept, and the first invalid field gets focus.

## 5. Style: tokens and icons only

- Colours, spacing, type, radius, shadow and z-index come only from `--vr-*` tokens in the Tokens
  section of `static/css/app.css`. Templates have no inline `style`, no hex, rgb or hsl, no
  raw px or rem, and no Bootstrap colour utility that app.css doesn't theme. Muted text is
  `.vr-muted`. `tests/test_css_tokens.py` and `tests/test_ui_standards.py` enforce this.
- Icons are Bootstrap Icons 1.13.2 symbols copied verbatim into `static/img/icons.svg` as
  `i-<name>`, rendered with `{% icon name="…" %}` or `window.vrUi.icon` in JS. Never draw an
  SVG, glyph or CSS shape. To add one, copy its `<symbol>` from the pinned package and run
  `tests/test_icons.py`.
- Scripts build buttons and icons with `window.vrUi.button` and `window.vrUi.icon`
  (`static/js/app.js`). The content security policy forbids inline scripts and styles.

## 6. Verify

```sh
set -a; . ./.env; set +a
uv run ruff check . && uv run ruff format --check .
uv run python manage.py check
uv run python manage.py collectstatic --noinput -v0
TEST_DATABASE_NAME=test_roots_<you> uv run pytest tests/test_ui_standards.py tests/test_css_tokens.py \
  tests/test_include_isolation.py tests/test_controls.py tests/test_icons.py tests/test_navigation.py \
  <the app's tests>
```

Then open the page on the dev server (http://127.0.0.1:8000, `demo_admin`, `demo_client` …) as
each role that sees it, in light and dark, at 1280px and a narrow width, with keyboard only
through the dialogs. For layout changes, run the browser checks for panel spacing, label
alignment and theme contrast (see the project-code-review skill for the commands).

## 7. Then document it

If any visible content, action, field, filter, permission or rule changed, run the
`document-page` skill and update `docs/pages/` in the same change. A pure markup refactor that
renders the same needs no help change.
