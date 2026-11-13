# Component and control reference

Source of truth: `COMPONENTS` in `core/templatetags/ui.py`, and the header comment of each
`templates/includes/<name>.html`, which documents its parameters, parts and an example. Read
that comment before using a tag. This table tells you which tag does which job (ui-standards §8).

## Contents

1. Components by job
2. Form controls
3. List columns and preferences
4. Buttons

## 1. Components by job (`{% load ui %}`)

Block tags end with `{% end<name> %}`. Parts are filled with `{% fill <part> %}…{% endfill %}`.

| Job | Tag | Notes |
| --- | --- | --- |
| Titled section or table card | `panel` (block) | `title`, `count`, `table=True` (flush table card), `hidden_title` (a list page's single card), `collapsible`/`open`, `class` for spacing between panels; parts `title`, `actions`, `count`, `footer` |
| Sub-block inside a panel body | `panel_section` (block) | Keeps the panel's inset and stack; `rule` for a separator |
| List body and its states | `list_results` (block) | Table and pagination, or the empty, no-results or filter-error state; `empty_title`, `action_url`/`action_label` for the create action |
| Notice inside a page | `notice` (block) | `tone` neutral, info, progress, success, warning, danger or hold; `blocking=True` gives role="alert" |
| Key facts under the title | `page_meta` (block) | Body is `<li>` items: status first, at most four facts |
| Header "More" menu | `more_actions` (block) | Beyond one primary and three secondary buttons; body is dropdown `<li>`s |
| Record layout | `record_layout` (block) | Parts `lead` (first panel) and `aside` (launch panel); body is the full-width panels |
| One fact row | `fact` | Inside `<dl class="vr-facts">`; blank shows "—" or `empty` |
| Form page or dialog body | `form_page` (block) | Errors, required note, sections, action row; `cancel_url`, `submit_label`, `destructive` |
| Form action row only | `form_actions` (block) | Body holds extra buttons |
| History | `history_panel` (block), `history_item` | Newest last, final panel; item has `title`, `at`, `actor`, `reason`, `changes` |
| Status of a record | `status` | `label`, `tone`, `plain`; the per-app state-to-tone mapping lives in a small partial |
| Time running out | `due_pill` | `due`, `soon_days` (30, or 7 for delivery targets), `plain` in cells; rule in `core.due` |
| Sample matrix | `matrix_tag` | `matrix`, `source`, `size` sm, lg or default |
| Icon | `icon` | `name` = Bootstrap Icons name present in `static/img/icons.svg` |
| Breadcrumbs | `breadcrumbs` | Records, forms, actions and documents only |
| On-this-page anchors | `page_toc` | Pages with four or more sections |
| One-button POST action | `post_button` | `action`, `label`, `variant`, `fields` |
| Destructive yes/no | `confirm_dialog` | Compact confirmation; use the form dialog when input is needed |
| Empty, no results, invalid filter | `empty_state` | Through `list_results` on lists |
| Toolbar, chips, columns | `grid_toolbar`, `filter_chips`, `grid_columns_button`, `grid_search` | One toolbar per list; `grid` = the table's `data-vr-grid` |
| Sortable column header | `sort_header` | `col`, `locked`, `hidden`, `priority="low"` |
| Pagination | `pagination` | Count, rows per page (25/50/100), pager |
| Row selection | `select_all`, `row_select` | Page-bounded; disabled rows show the reason |
| Form pieces | `form_errors`, `form_sections`, `required_note`, `field` | Usually via `form_page` |
| Brand and footers | `brand`, `record_foot`, `source_footer` | Shell and page foot |

Shell and page includes that are not registered tags (plain `{% include %}`) include
`includes/section_tabs.html` (section tabs), `includes/messages.html` and `includes/form_dialog.html`.

## 2. Form controls (ui-standards §8, Form controls)

| Field | Use | Never |
| --- | --- | --- |
| Choice (few options) | `forms.ChoiceField` / `ModelChoiceField`: Tom Select via the select widget template; up to eight options without a search box | A raw `<select>` in a template |
| Large or growing model choice | `ModelChoiceField(queryset=…, widget=core.lookups.RemoteSelect("<lookup>"))` and `register(Lookup("<name>", staff(lambda request: …), SEARCH_FIELDS, ("name", "pk")))` in `core/lookups.py` (fields: name, queryset(request), search_fields, ordering, label, page_size) | Rendering every option inline; a lookup queryset that skips the page's authorization |
| Multiple choice | `SelectMultiple` (Tom Select chips); short lists `CheckboxSelectMultiple` | |
| Free text with suggestions | `core.widgets.SuggestInput(values)` | `<datalist>` |
| Date, date and time | The project's date field widgets (typed `YYYY-MM-DD[ HH:MM]`, calendar popover, lab-local time) | `type="date"` / `datetime-local` |
| Number | Text input with `inputmode`; unit via `attrs={"unit": "…"}` | `type="number"`, spinners, float conversion |
| File | The file widget (drop zone) | A bare file input |
| Checkbox, radio, switch | Bootstrap `form-check` through the widget | |

Every form is `novalidate` (set by `controls.js`). The server's messages are the only errors.

## 3. List columns and preferences

- Table: `<table class="table table-hover vr-table" data-vr-grid="<app>.<list>" data-vr-grid-version="1">`.
- Headers: `{% sort_header label=… col="<id>" … %}` with `locked=True` on the identifier and
  actions columns, and `priority="low"` on columns that can hide below 1366px.
- The first cell is `<th scope="row"><a class="vr-row-link" href="…">identifier</a></th>`.
  Identifiers, dates, figures and statuses don't wrap. Numbers are right-aligned with the unit
  in the header. Dates go in `<time>`.
- Saved per user (`core/preferences.py`): `grid:<grid>:v<version>` holds column order and hidden
  columns (`static/js/grid.js`); `page:<view name>` holds `per_page` and `sort` (middleware).
  A saved value that no longer fits is ignored, so bump `data-vr-grid-version` when column ids
  change. Preferences are presentation only and never widen what a user may see.

## 4. Buttons

| Role | Classes | Where |
| --- | --- | --- |
| Primary | `btn btn-primary` | One per page or form |
| Secondary | `btn btn-outline-secondary` | Header, toolbar, card header |
| Quiet | `btn btn-link` | Cancel, Clear all |
| Destructive | `btn btn-outline-danger` | First in its row; the label names its object |
| Row | add `btn-sm` | Table cells and card headers only |

Header buttons carry an icon only to create (`plus`) or to download or print.
