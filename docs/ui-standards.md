# UI standards

Baseline: 2026-11-10. These rules apply to every staff and client-portal page.
Code is the reference for how a component is built; this document says
which component does which job and how pages are put together. Basis: a census of every rendered
page, per-area source audits and a review of published design-system guidance (Carbon, PatternFly,
Primer, Cloudscape, GOV.UK, USWDS, Fluent, NN/g, WAI-ARIA APG, Bootstrap 5.3) on that date. Where
the published guidance splits, the rule below is a house decision and says so.

## 1. Library and principles

- Bootstrap 5.3 is the only component library. Controls, dialogs, dropdowns, collapses, tables
  and form widgets are Bootstrap components, themed through the `--vr-*` tokens in
  `static/css/app.css`, which map onto Bootstrap's `--bs-*` variables. DataTables presents grid
  columns, Tom Select draws every select and suggestion input, Vanilla Calendar Pro draws the
  calendar of the date fields, Chart.js draws control charts. Nothing else, and no self-written
  replacement for something these provide. No control shows the browser's own UI (section 8,
  Form controls); the library's own icons and arrows are replaced by Bootstrap Icons symbols.
- One job, one component. Each concept in section 8 has exactly one shared include or one markup
  pattern. A page that needs a variation asks for a parameter on the shared part; it does not
  copy the markup.
- Templates carry no colours, no inline `style`, and no Bootstrap colour utilities that bypass
  the tokens (`bg-white`, `bg-light`, `text-dark`, `text-black`, `table-light`, and any other
  `text-*`, `bg-*`, `border-*`, `link-*`, `btn-*` or `table-*` colour that `app.css` does not
  theme). Muted text is `.vr-muted`, never `text-body-secondary` or `text-muted`.
- Tokens. The Tokens section at the top of `static/css/app.css` defines every colour and length
  once, for light, dark and print; every rule after it uses the tokens:

  | Scale | Tokens |
  | --- | --- |
  | Spacing | `--vr-space-N` = N x 0.25rem: 0-5, 1, 1-5, 2, 2-5, 3, 3-5, 4, 5, 6, 8, 10, 12 |
  | Type | `--vr-font-sans` and `--vr-font-data` (Overpass; data adds tabular figures and a slashed zero), `--vr-font-mono` for code; `--vr-font-size-1` (0.75rem) to `-9` (1.875rem, the page title) |
  | Font weight | `--vr-font-weight-regular` 400, `-medium` 500, `-semibold` 600, `-bold` 700, `-black` 800 (titles) |
  | Line height | `--vr-line-height-tight` 1.2, `-snug` 1.3, `-base` 1.5; `--vr-tracking-title`, `--vr-tracking-label` |
  | Radius | `--vr-radius-sm` 4px, `--vr-radius` 8px, `-lg` 10px (fields, rows, nav), `-button` and `-menu` 12px, `-lane` 14px, `-xl` 16px (panels), `-dialog` and `-main` 18px, `-pill` |
  | Line width | `--vr-border-width` 1px, `-md` 1.5px (checkboxes), `-lg` 2px, `-xl` 3px (station rings); `--vr-line-width` 4px (line diagram); `--vr-focus-width`, `--vr-focus-offset`, `--vr-halo-width` |
  | Shadow | `--vr-shadow-panel`, `-raised` (menus), `-dialog`, `-button`, `-current`, `-scroll-top`, `-scroll-bottom`; `--vr-field-focus-ring`, `--vr-field-invalid-ring` (none in print) |
  | Stacking | `--vr-z-below`, `-base`, `-raised`, `-sticky`, `-topbar`, `-menu`, `-skip` |
  | Sizes | `--vr-control-height` (36px), `-sm` (30px), `--vr-nav-item-height`, `--vr-cell-py`/`-px`, `--vr-badge-*`, `--vr-topbar-height`, `--vr-sidenav-width`, `--vr-shell-gutter`, `--vr-scrollbar-*`, `--vr-form-width` |

  rem steps follow a 16px root. Bootstrap's semantic colours
  (`--bs-primary`, `secondary`, `info`, `success`, `warning`, `danger` with their `-rgb`,
  `-text-emphasis`, `-bg-subtle` and `-border-subtle` forms) map onto the `--vr-*` tones, and the
  button and table variants in use (`btn-danger`, `table-danger`, `table-secondary`) are themed.
  A one-off dimension of a single component (a column width, a logo's maximum size) may stay a
  literal; `tests/test_css_tokens.py` lists those properties with the reason.
- Scripts build markup through the same parts: `window.vrUi.icon(name)` (the markup of
  `includes/icon.html`) and `window.vrUi.button(label, role, small)` (the button roles below),
  both in `static/js/app.js`.
- Icons. Every icon is a Bootstrap Icons 1.13.2 symbol (the version pinned in
  `static/vendor/README.md`), copied verbatim into `static/img/icons.svg` as `i-<bootstrap-name>`,
  and rendered only through the `{% icon %}` tag (`includes/icon.html`) in templates or
  `window.vrUi.icon` in scripts.
  Status glyphs are Bootstrap Icons too (`includes/status.html`). No inline `<svg>`, no hand-drawn
  glyph, text character or `clip-path` shape standing in for an icon, and no custom SVG data URI;
  `app.css` keeps only Bootstrap's own form-control images. The drawings that are not icons are
  the brand mark (`static/img/mark.svg`), the sign-in scene (`static/img/entrance/scene-light.svg`
  and `scene-dark.svg`, built by `tools/entrance_scene/`, a CSS background by theme) and
  the line diagram's CSS dots, rings, ticks and stubs (section 12). To add an icon, copy its `<symbol>` from the pinned
  `bootstrap-icons` package (`icons/<name>.svg`: its `viewBox` and inner markup, unchanged) into
  `icons.svg`, then run `tests/test_icons.py`.
- One term per thing. The sidebar label, the page title, the breadcrumb and the button that
  leads to a page use the same words (section 9).
- The server decides. Hidden controls are a courtesy; every rule here sits on top of server-side
  authorization and validation, never in place of them.

## 2. Page types

Every page is one of these. The type fixes its header, layout and navigation behaviour.

| Type | Purpose | Breadcrumbs | Primary action | Examples |
| --- | --- | --- | --- | --- |
| List | Find and open records | No | `New <thing>` in the header | Samples, Quotes, Equipment |
| Record | Read one record, start actions on it | Yes | The next step for the record's state, in the header | Sample, Batch, Report |
| Form | Create or edit one record in a dialog | Fallback page only | The submit button, in the form's action row | New client, Edit equipment |
| Action form | One audited change in a dialog | Fallback page only | The submit button, in the form's action row | Change disposition, Record transfer |
| Entry grid | Enter many values in a wide, maximizable dialog | Fallback page only | The save button, in a sticky action row | Enter results, Record measurements |
| Overview | Work queues and counts that link to filtered lists | No | None, or the one most frequent start | Dashboard, Lab overview |
| Document | A preview of something issued or exported | Yes | The decision about the document, in the header | Report preview, Quote |

## 3. Navigation

1. The sidebar owns work destinations. Show destinations for the account’s effective roles and
   explicit capabilities, combine them across roles, and omit empty sections. Existing record
   links and direct pages retain their authorization even when their destination is absent from
   the sidebar. A destination appears in exactly one navigation component: never in the sidebar
   and again as tabs, pills or header buttons.
2. Section tabs exist for one case only: one sidebar item whose content is two to six sibling
   lists that are not themselves sidebar items (Catalog: Tests, Methods, Analytes; Inventory:
   Lots, Materials), or sibling sub-views of one action (the sections of a sample correction).
   They are links with their own URLs, rendered by `includes/section_tabs.html` directly under
   the page header, with `aria-current="page"` and no tab roles. One visual style. Tabs stay
   visible on every list in the set and never appear on record or form pages.
3. A long page with four or more sections gets an "On this page" list of anchors
   (`includes/page_toc.html`). It is not a tab set and does not hide content.
4. Each list, overview, record, form and action page maps to one sidebar item. Mark that item
   current only when it is visible for the account; a permitted page whose destination is absent
   marks none. Pages reached from the user menu (password, two-factor) also mark none. The
   mapping from route to sidebar item is declared in one place and checked by a test (section
   11). A route shared by two kinds of record (QC pages of an analytical or a preparation batch)
   is declared as shared, and the view names the item for the record at hand.
5. Breadcrumbs appear on every record, form, action, entry and document page, and never on lists
   or overviews. The trail is `Sidebar item › Record identifier › Action`; the last crumb is the
   current page and is not a link. Under a tabbed section the trail is `Section › Tab › …`, where
   the section is text and the tab links to its list. Rendered by `includes/breadcrumbs.html`.
   There is no separate Back link.
6. Cross-links to another section belong in the content where they are relevant (a batch number
   in a table cell), not in the page header as a button.
7. Signed-in users keep the application shell on error pages (400, 403, 404).
8. The client portal has a top navigation bar and no sidebar. The same rules apply: one current
   item, including Home on the landing page.

## 4. Page header

Top to bottom, all through the blocks of `templates/base.html`:

| Part | Block | Rule |
| --- | --- | --- |
| Breadcrumbs | `breadcrumbs` | Section 3, rule 5 |
| Title | `page_heading` | List and overview: the sidebar label. Record: the record's identifier. Form: `New <thing>` or `Edit <identifier>`. Action: the same words as the button that opened it |
| Key facts | `page_meta` | Record pages: status first (section 8), then at most four facts that identify the record. Not used for prose |
| Description | `page_intro` | At most one sentence, under the title. Explanatory text never floats between the header and the first panel |
| Actions | `page_actions` | Right of the title on one row at every width from 992px. Order left to right: destructive (outline danger), secondary actions, primary last. At most one primary and three secondary; anything further goes in a `More` dropdown |

- A button-only action (no input needed) that changes the record's state lives in the header.
  An action that needs input opens a form dialog with its own action row.
- A header button carries an icon only when it creates something (`plus`) or downloads or prints
  a file. No other header icons.
- A list has one primary button on the page: the header's `New <thing>`. The toolbar's `Apply`
  and an empty state's action are secondary.

## 5. Lists

The server sorts, filters and paginates the full authorized queryset; DataTables only shows,
hides and reorders columns. One list is one `section.card.vr-table-card`.

1. Toolbar. One toolbar, `includes/grid_toolbar.html`: search (labelled "Search"), up to three
   everyday filters, `More filters` (a Bootstrap collapse, open when one of its filters is
   active), `Apply`, `Clear all` when anything is active, then `Columns` at the right. Active
   filters show as removable chips under the toolbar. A default value is not an active filter.
2. Columns. Every grid has a `Columns` button, on staff and portal pages alike. A column may carry
   a low priority (`data-vr-col-priority="low"`): it is hidden by default on screens narrower than
   1366px so names keep their room, the user can show it in `Columns`, and a saved layout wins.
   Every standard table, list or record sub-table, fits its card at 1280px and 1440px: column
   heads wrap between words, lists in a cell wrap their items, and a record sub-table without a
   `Columns` control marks its secondary columns the same way (`data-vr-col-priority="low"` on the
   head; `app.js` marks the column's cells), hidden below 1366px while the record keeps the full
   data. Only entry grids and genuinely wide data tables (the laboratory's certificate list)
   scroll sideways, with a visible scrollbar and the frozen-column edge shadow.
   Column layout, rows per page and sort are saved per user on the server (`core/preferences.py`:
   lab workstations are shared, so nothing is kept in the browser); a list opened without
   `per_page` or `sort` uses the user's saved values, and `Clear all` removes filters only.
   Rows are at most two lines; identifiers, dates, figures and statuses never wrap inside
   themselves, and a truncated value carries its full text in a tooltip and for screen readers. Identifier first
   and locked; status next to the identifier in queues; numbers right-aligned with the unit in
   the header; dates as `YYYY-MM-DD HH:MM` in a `<time>` element; actions last and locked.
3. Opening a record. The identifier in the first column is the link to the record page
   (`th[scope=row] > a.vr-row-link`). It is the only way a row opens: no `Open`, `View` or
   `Details` link, no whole-row click, no `Edit` button standing in for it.
   - The link text is the row's own identifier (the lot number on a lot, not its material), and
     an identifier always leads to the record it names, on every list where it appears.
   - Where a record has no page of its own (catalog tests, methods and analytes, price lists,
     projects, import mappings, QC rules), the identifier links to the edit form for users who
     may edit, and is plain text for everyone else. House decision: published systems do not
     settle this case, and one rule for every grid is worth more than a second pattern.
   - Queue and count tables on overviews follow the same rule: the label is the link.
4. Row actions. Only for something done to the row that is not opening it. At most one inline
   button (`btn btn-sm btn-outline-secondary`), the next step for that row's state. Two or more
   go in an `Actions` dropdown. A destructive row action is a button that asks for confirmation,
   never a link that posts.
5. Selection. One mechanism, `includes/select_all.html`: a header checkbox bounded to the page,
   a count, and a bulk action bar. Ineligible rows are disabled with a visible reason.
6. Footer. `includes/pagination.html`: count, rows per page (25, 50, 100), pager. Shown even
   when everything fits on one page; replaced by the empty state when there are no rows.
7. States. `includes/empty_state.html` in place of the table: `empty` with the create action for
   users who may create, `no-results` with `Clear filters`, `error` when a filter is invalid. No
   placeholder rows, no bare "None" paragraphs.
8. Several tables on one page. Each is its own table card with an `h2` in the card header, its
   count, and its `Add <thing>` button in that header. The page has no primary button.

## 6. Record pages

1. Order of content: header; alerts that need action; facts; related tables; history last.
2. Facts are `dl.vr-facts` inside a panel with an `h2`. Never a Bootstrap grid of labels, never
   a two-column table.
3. Each related list is a table card following section 5, with its `Add <thing>` button in the
   card header. A related record opens through its identifier link.
4. History is one component, `includes/history_item.html`, newest last, in the final panel:
   what happened, when, who, and the reason where one was required.
5. Layout is `{% record_layout %}` (`includes/record_layout.html`). A side column (from 1200px)
   may hold the context and launch panel of the most frequent record action. It sits beside the
   first panel only (the record's key facts) and grows with its content up to the viewport
   height. Longer content scrolls inside its own keyboard-focusable box with a visible scrollbar
   and a fade while more remains below; it never hides content silently. Long-form guidance does
   not belong in the side column. Every later panel spans the
   full width, so wide tables keep their columns. Below 1200px the page stacks: first panel, side
   column, then the rest, which is also the reading and tab order. Its editing fields open in the
   shared form dialog. No panel inside a panel.
6. `Edit` is a secondary header button for records that can be edited freely. A locked record
   is changed through `Correct <thing>`, which requires a reason and is recorded in history.

## 7. Forms

House decision, approved 2026-11-10: use one Bootstrap modal system for editing, including
record-local action forms. A drawer would split equivalent workflows into two interaction
patterns. The record or list stays behind the dialog; long forms and entry grids can be maximized.
Small screens use the full screen. Normal dialogs have comfortable outer and inner margins;
the heading and close controls remain visible while the body scrolls. Fields use treatment A of
the Evergreen design (section 12): no fill, a rounded 1px outline, a green ring with a soft halo
on focus, and the danger outline with its message under the field when invalid.

Use one dialog at a time. Move focus inside on open, retain keyboard focus there, and return it
to the opener on close. Escape and Cancel close clean forms; unsaved input offers Keep editing
or Discard changes. Loading, failed requests and validation have visible states. Validation
keeps entered values and focuses the first invalid field. Save follows the existing server
destination; multi-step forms and Add another remain in the dialog. Direct form URLs open the
same dialog, with a server-rendered page as the no-JavaScript fallback. Authentication,
authorization, CSRF, concurrency and scientific validation stay on the server.

This follows [Bootstrap's scrollable and fullscreen modal patterns](https://getbootstrap.com/docs/5.3/components/modal/)
and [WAI-ARIA dialog keyboard and focus guidance](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/),
reviewed 2026-11-10. Choosing dialogs for every editing task is this application's consistency
decision, not a claim that all applications should do so. Read-only record pages, review queues,
report previews, search/filter controls and account sign-in remain ordinary workspaces.

1. Rendering. Django forms through the project renderer (`{{ form }}`, `as_field_group`,
   `includes/field.html`). No hand-written inputs where a form field exists.
2. Structure. `form.vr-form` containing one or more `fieldset.vr-form-section`, each with a
   legend when there is more than one. One column; two columns from 768px only for short related
   pairs; notes full width. Field width follows the expected value.
3. Required fields carry `*` after the label, and the form says "Fields marked * are required."
   once at the top. House decision; the published systems split on this.
4. Errors. `includes/form_errors.html` at the top with `role="alert"`, the message under each
   field, focus on the first invalid field, and every entered value kept.
5. Action row. `div.vr-form-actions`, right-aligned and sticky at the bottom of the viewport:
   destructive action first (`btn-outline-danger`), then `Cancel` (`btn btn-link`), then the one
   primary button last. House decision; the published systems split and consistency is what
   matters. Dialog action rows stick to the bottom of their scrollable body. Dropdown and
   sign-in action rows remain in normal flow.
6. Destinations. After saving, the record page; where the record has no page, the list. `Cancel`
   returns to the page the user came from: the record when editing, the list when creating.
7. Entry grids follow rules 4 to 6 and are never DataTables-enhanced: fixed columns, units in
   the header, tab order left to right then down, row-level messages under the row.
8. Child rows (containers, quote lines) use one pattern: a row per item, an `Add <thing>`
   button under the rows, and a `Remove` control on each row that works without JavaScript.

## 8. Components

| Job | The one way to do it |
| --- | --- |
| Status of a record | `{% status %}` (`includes/status.html`: text, Bootstrap Icons glyph, tone); the mapping from a domain state to one of seven tones lives in one small partial per app |
| Panel or table card | `{% panel %}` (`includes/panel.html`): `h2`, count, header actions, body, footer; `table=True` for a table card |
| List rows or their state | `{% list_results %}` (`includes/list_results.html`): the table and pagination, or the empty, no-results or filter-error state |
| Notice inside a page | `{% notice %}` (`includes/notice.html`): the tone's alert variant opening with `includes/status.html` in its `plain` form; `role="alert"` when it blocks the task, otherwise `role="status"` |
| Result of the last action | Django message at the top of the next page (`includes/messages.html`, rendered as a notice); it stays until dismissed or the next navigation. No toasts: a failed background request shows a dismissible danger notice in the same place (`<template id="vr-request-error">`) |
| Breadcrumbs | `{% breadcrumbs %}` (`includes/breadcrumbs.html`) |
| Section tabs | `includes/section_tabs.html` |
| On-this-page anchors | `{% page_toc %}` (`includes/page_toc.html`) |
| Key facts under the title | `{% page_meta %}` (`includes/page_meta.html`) |
| List toolbar, chips, columns | `{% grid_toolbar %}`, `{% filter_chips %}`, `{% grid_columns_button %}` |
| Sortable header | `{% sort_header %}` (`includes/sort_header.html`) |
| Pagination and count | `{% pagination %}` (`includes/pagination.html`) |
| Empty, no results, filter error | `{% empty_state %}` (`includes/empty_state.html`), through `{% list_results %}` on lists |
| Row selection | `{% select_all %}` for the header, `{% row_select %}` for each row |
| Facts | `dl.vr-facts` of `{% fact %}` rows (`includes/fact.html`; a blank value shows "—") |
| History | `{% history_panel %}` (`includes/history_panel.html`) of `{% history_item %}` entries |
| Record page layout | `{% record_layout %}` (`includes/record_layout.html`): the first panel with the side column beside it, then full-width panels |
| Form page | `{% form_page %}` (`includes/form_page.html`): errors, required note, sections, action row |
| Form action row | `{% form_actions %}…{% endform_actions %}` (`includes/form_actions.html`); the body holds any further buttons |
| Required-fields note | `{% required_note %}` (`includes/required_note.html`) |
| Form error summary | `{% form_errors %}` (`includes/form_errors.html`) |
| One-button POST action | `{% post_button %}` (`includes/post_button.html`) |
| Disclosure of filters | Bootstrap collapse, through the toolbar include |
| Disclosure of a note, long value or collapsible panel | `<details>` with a `<summary>`, styled once in `app.css` (chevron marker, focus ring); `{% panel collapsible=True %}` for a panel |
| Menu of actions | Bootstrap dropdown |
| Editing or an action requiring input | Shared maximizable Bootstrap form dialog, with the object named |
| Confirmation of a destructive action | Compact Bootstrap confirmation dialog for a single yes or no; shared form dialog when input is required |
| Brand mark and name | `{% brand %}` (`includes/brand.html`) |
| Icon | A Bootstrap Icons symbol in `static/img/icons.svg`, through `{% icon %}` (`includes/icon.html`); `window.vrUi.icon` in scripts |
| Hint on hover and focus | A `title` attribute; `static/js/controls.js` turns it into a Bootstrap tooltip and makes a non-interactive hint focusable. A `<time>` keeps its title tooltip out of the tab order; `iframe` and `abbr` keep the native title |

Every shared component is a tag of the `ui` library (`{% load ui %}`), declared once in
`COMPONENTS` in `core/templatetags/ui.py`: its template under `templates/includes/`, whose header
comment documents it, its keyword parameters and the page variables it is documented to read. A
tag renders in a context of its own: the parameters it is given (an omitted one is empty, never a
page variable of the same name), its documented reads, and `request`, `user` and `csrf_token`.
Pages never `{% include %}` a registered component; `tests/test_include_isolation.py` checks
that no component reads a page variable it was not given.

Form controls. No control shows the browser's own UI. Each is one Django widget template in
`templates/django/forms/widgets/` plus one initialiser in `static/js/controls.js` and token-only
CSS in the Form controls section of `app.css`; pages get it by rendering a form field, never by
writing the element (`tests/test_controls.py` fails on a raw `<select>`, `<datalist>` or
date/time/number/file `<input>` in a page template). The native element stays in the form as
the no-JavaScript fallback and the submitted value, and the server stays the validator.

| Field | Control |
| --- | --- |
| Choice (`select`) | Tom Select. Up to eight options: no search box; type-ahead, and Space or Enter opens the menu, as a native select. More than eight: a search box in the menu. The empty choice stays selectable. A select that may be empty (a filter's "Any …", an optional field, a multi-select) has a clear button, `Clear <label>`, beside the chevron, shown while the control is hovered, focused or open (always on touch screens) and only when there is a value; Backspace or Delete on the closed control also clears. Clearing fires the usual change event (filters still wait for Apply); a required select has none. The menu is fixed beside its field, so a dialog or table never clips it |
| Large or growing model choice | `core.lookups.RemoteSelect("<lookup>")`: Tom Select loading pages from `/lookups/<lookup>/` as the user types. Each lookup in `core/lookups.py` builds its queryset from the request (staff roles, or the portal user's own client); the form field's queryset still validates, and an edit form renders its current value |
| Multiple choice (`SelectMultiple`) | Tom Select with removable chips. Short lists stay Bootstrap check groups (`CheckboxSelectMultiple`) |
| Free text with suggestions | `core.widgets.SuggestInput` (values or `(value, label)` pairs): Tom Select free text; what is typed is the value |
| Date, date and time | A typed text field in the format the server reads (`YYYY-MM-DD`, `YYYY-MM-DD HH:MM`, with `:SS` where the field records seconds; US `MM/DD/YYYY` is also read), with a calendar popover (Alt+Down, Esc closes and returns focus; weeks start on Sunday). Shortcuts Today, Yesterday, Clear; Now, Today, Clear for date and time. Times are the laboratory's local time, stated under the field; shortcuts use the laboratory's clock, never the browser's zone. An unreadable or out-of-range date stops submission with the message under the field |
| Number | A text field (`inputmode` numeric or decimal where the value cannot be negative), so the exact typed string reaches the Decimal field; no spinners. A unit is the widget attr `unit` (`forms.NumberInput(attrs={"unit": "%"})`): one input group with the unit as its addon, read with the field, wherever the field renders; the label carries no unit |
| File | A drop zone that names the chosen file and its size and lists the accepted types; the native input keeps keyboard focus |
| Search box | One clear button; the browser's own is hidden |
| Checkbox, radio, switch | Bootstrap `form-check`, themed by tokens |
| Validation | Every form is `novalidate` (set centrally by `controls.js`): no browser bubbles; errors are the server's inline messages |

Buttons:

| Role | Classes | Where |
| --- | --- | --- |
| Primary | `btn btn-primary` | One per page or form |
| Secondary | `btn btn-outline-secondary` | Header, toolbar, card header |
| Quiet | `btn btn-link` | `Cancel`, `Clear all` |
| Destructive | `btn btn-outline-danger` | Header or action row, first in the row |
| Row | add `btn-sm` | Table cells and card headers only |

## 9. Wording

| Meaning | Words | Notes |
| --- | --- | --- |
| Create a top-level record | `New <thing>` | List header. The form's title is the same; its submit is `Create <thing>` |
| Attach a child to the open record | `Add <thing>` | Card header or under the child rows |
| Log something that happened | `Record <event>` | Transfers, measurements, calibrations, disposal |
| Take samples in | `Receive <thing>` | The laboratory's own verb; kept |
| Change a freely editable record | `Edit` in the header; `Edit <identifier>` as the title; `Save changes` to submit | |
| Change a locked record with a reason | `Correct <thing>` | Title and submit use the same words |
| Move a record to another state | `Change <state>`, or the state's own verb (`Close batch`, `Release version 2`, `Withdraw quote`) | Not `Update` |
| Leave a form without saving | `Cancel` | Never used alone for anything else |
| End or void a record | Verb plus object (`Cancel chain of custody`, `Delete draft`) | A destructive label always names its object |
| Detach a child | `Remove` | |
| Apply filters / reset them | `Apply` / `Clear all` | |

Sentence case everywhere. A title, its breadcrumb, its sidebar item and the button that opens
it use the same words. Staff and portal pages use the same term for the same thing unless the
client's point of view needs another ("Your sample ID").

## 10. Roles and states

- A control the user's role can never use is not rendered. A control that is unavailable only
  in the record's current state is rendered disabled with the reason in text beside it.
- Review pages show the reviewer's own work as read-only with one sentence, the same wherever it
  occurs: "You analysed this. Another qualified reviewer must review it."
- Every page has its states: empty, no results, invalid filter, validation error, denied and
  read-only. Loading states apply only where a request updates part of a page.

## 11. Enforcement

- `tests/test_ui_standards.py` scans every template for the patterns this document retires:
  colour literals and inline styles, banned utilities, pill navigation, hand-written pagination,
  status spans, breadcrumbs and toolbars outside the shared includes, `Open` and `View` row
  links, more than one primary button in a page header. It also holds the component rules of
  section 8: card markup outside `{% panel %}`, `.alert` outside `{% notice %}`, hand-written
  list states, history timelines, `<dt>` rows and key-fact lists, the required-fields sentence and
  action rows outside their includes, visible `<input>` elements outside form widgets and the
  shared includes, and colour utilities `app.css` does not theme (including
  `text-body-secondary`).
- `tests/test_css_tokens.py` checks `static/css/app.css`: no hex, `rgb()` or `hsl()` colour and no
  px or rem length after the Tokens section (except the listed one-off dimensions), every
  `var(--vr-*)` used is defined, and every `-rgb` channel token matches its colour.
- `tests/test_include_isolation.py` renders every shared component tag with only its explicit
  arguments, with and without decoy page variables of the generic names pages use (`form`,
  `class`, `title`, `cancel_url` ...), and fails when the output differs. Each component is a
  `{% load ui %}` tag (`core/templatetags/ui.py`) rendering in a context of its own, because a
  plain Django include sees its caller's whole context; `tests/test_ui_standards.py` forbids a
  plain `{% include "includes/<component>.html" %}` for every registered component.
- `tests/test_icons.py` checks the icon rule: no `<svg>` in templates other than `icon.html`, no SVG built in scripts other than `vrUi.icon`, sprite ids named
  `i-<bootstrap-name>` on Bootstrap's 16-unit grid, every icon name used in code present in the
  sprite, only allow-listed SVG data URIs in `app.css`, and status glyphs drawn by icons.
- `tests/test_navigation.py` resolves every HTML route and asserts that it marks exactly one
  sidebar or portal item (or is declared as an account page), and that the item's label equals
  the list page's title.
- The browser suite keeps one journey per page type and the contrast check for both themes.

## 12. Visual language

Evergreen, approved by the owner 2026-11-13 (Transit · Evergreen, field treatment A; mockups and
the value table in `.local/mockups/evergreen/`). It replaces the 2026-11-09 "field notebook" and
settles the earlier conflict between rounded and square shapes: everything is rounded and
borderless. `static/css/app.css` holds the one definition of each token and component for light,
dark and print; this section states the intent and the rules.

**Separation by surface, never by lines.** The shell, the content ground, panels, raised layers
and hover each have their own tint. Panels, table rows, the shell and dialogs have no outlines.
The only lines are field outlines, checkbox and radio edges, focus rings, the invalid outline and
the coloured lines of the line diagram. No pure black or white on screen.

**Palette roles.**

| Role | Tokens | Used for |
| --- | --- | --- |
| Shell | `--vr-topbar-*`, `--vr-shell-chip`, `--vr-shell-hover`, `--vr-shell-current`, `--vr-on-shell-current` | The top bar and sidebar, one surface: pale sage by day, forest night in dark mode; the laboratory chip; nav hover; the current nav item (forest fill) |
| Ground and panels | `--vr-paper`, `--vr-surface`, `--vr-surface-2`, `--vr-surface-hover`, `--vr-raised` | The content sheet (rounded where it meets the shell); panels and table cards; counts, wells and history items; hover; menus, dialogs and popovers |
| Text | `--vr-text`, `--vr-text-strong`, `--vr-text-muted`, `--vr-placeholder` | Body text; titles and emphasised values; secondary text; placeholders |
| Action (forest) | `--vr-ink`, `--vr-on-ink`, `--vr-pen`, `--vr-focus`, `--vr-focus-halo` | The primary button, the current page and checked controls; links; focus |
| Selection | `--vr-selection` | Selected rows, filter chips, matched text, text selection |
| Fields | `--vr-border-strong`, `--vr-border-strong-hover`, `--vr-danger-border` | The 1px field outline and checkbox edge (3:1 on every surface); its hover; the invalid outline |
| Tables | `--vr-th-bg`, `--vr-row-stripe`, `--vr-row-hover` | The header row fill, the faint zebra, the hover fill |
| Status tones | `--vr-{neutral,info,progress,success,warning,danger,hold}-{text,bg}` | Status pills and notices only |
| Lines | `--vr-line-{intake,sales,bench,release,lab,setup,pending}`, `--vr-lane`, `--vr-count` | The line diagram and the sidebar group stubs: accents for laboratory areas, never status |
| Red pencil | `--vr-pencil` | The required-field mark and struck prior values |

**Type.** One family, Overpass (OFL, self-hosted in `static/vendor/overpass-5.3.0`), with a little
extra word spacing. Data (identifiers, dates and times, counts, right-aligned figures) uses the
same family with tabular figures and a slashed zero (`--vr-font-data`). The system monospace is
for code only. Page titles are 30px weight 800 with slight negative tracking; dialog titles 24px;
panel titles 16px bold; body 14px; tables 13px; labels 13px bold. Uppercase tracked labels appear
only in sidebar group headings and table heads.

**Shape and elevation.** Fields, nav items, table rows and menu options 10px radius; buttons and
menus 12px; panels and notices 16px; dialogs and the content sheet's corner 18px; pills fully
round. Elevation is soft: panels a faint two-layer shadow, menus and popovers a menu shadow,
dialogs a deeper shadow over a scrim; the primary button a slight lift. No hard offsets.

**Components.**

- Shell: the brand lock-up, the laboratory name as a tonal pill, the sample lookup as a field on
  the shell, quiet Help, Theme and Account buttons. The sidebar keeps a gutter so its scrollbar
  stays inside the shell. Group labels open with a short stub in the area's line colour; the
  current item is the forest fill with bold text and `aria-current="page"`.
- Page header: breadcrumbs on records only; the title; key facts as a row of muted text with the
  status pill first; actions right, aligned to the foot of the title block (secondary buttons,
  then the one primary). An overview's dateline sits under the title, never above it.
- Panels: the surface fill, 16px radius, the panel shadow; the head carries the title, its count
  as a pill and tools on the right, with no rule under it. Spacing is defined once in the panel
  component: every panel body (`.vr-panel-body`, also each `{% panel_section %}`) insets its
  content by `--vr-panel-inset` and separates stacked blocks by `--vr-stack`; a table is flush only
  as a table panel's own body (`.vr-table-body`), where notes and selection bars still take the
  inset; forms inside a panel lie on it without a surface of their own. `e2e/test_panel_spacing.py`
  (measurement in `e2e/panel_spacing.js`) checks the main record and list pages.
- Key facts: one grid of equal tracks (`--vr-fact-width` at least), so labels and values line up
  from row to row; a long value wraps inside its cell, and a fact whose value is a phrase (a
  project, a location, a preservation check) is `{% fact wide=True %}` and spans two tracks, or the
  row when only one fits. `e2e/panel_spacing.js` checks every fact starts on a track line.
- Action rows: on a page the row is plain, in flow directly after its fields. Only a form
  dialog's footer is sticky and carries the raised-edge shadow, and it is always the dialog's last
  element; nothing else in a dialog is raised. Raised edges (dialog header and footer, a frozen
  table column) use one shadow family (`--vr-shadow-scroll-*`).
- Tables: no vertical or per-row rules; a rounded header row in `--vr-th-bg` with 12px bold
  uppercase labels; rows with rounded ends, a faint zebra and a hover fill. The sorted column's
  label is in the strong text colour with its arrow. The identifier link is forest, bold, and
  underlined only on hover.
- Status: a tinted pill (tone fill, tone text, a Bootstrap Icons glyph, 12px bold), no edge;
  inside a table cell it may wrap with a 10px radius; inside a notice it drops its fill.
- Notices: the tone's fill, 16px radius, no border; one grid: the status in its own column, its
  pill centred on the content's first line, labelled lines as a two-column `dl` of `{% fact %}`
  rows.
- Wide tables: the identifier column stays frozen while the table scrolls sideways, with its row's
  opaque fill and an edge shadow only while scrolled; full-width sub-rows (`td.vr-subrow` holding
  `.vr-subrow-content`) stay pinned to the visible width. A free-text row header (`th.vr-wrap`, a
  finding message) is not an identifier and is not frozen.
- Buttons: primary is the forest fill; secondary (`btn-outline-secondary`, kept as the class
  contract) a tonal fill with no outline; quiet (`btn-link`) forest text underlined on hover;
  destructive the danger tint, solid on hover. 36px tall, 30px small.
- Fields (treatment A, the only field style): see section 7. Checkboxes and radios are
  Bootstrap's own, with a 1.5px edge and forest when checked.
- Dialogs: the raised surface, 18px radius, the dialog shadow and scrim, no rules; the header
  takes a soft shadow while the body is scrolled, and the sticky action row always carries one.
- Scrollbars: thin, rounded and inset on a transparent track; stronger when the container is
  hovered, strongest on the thumb. Never hidden, with one exception: the sidebar's scrollbar is
  quiet at rest and fades in on hover or focus (no fade under reduced motion). Tables and panels
  keep a visible scrollbar, so overflow is never silent.

**The line diagram.** Each laboratory area has a line colour. On the dashboard its work queues
are stations on a lane: a ring with the queue link, its status pill, the oldest item and a count
badge when work waits; a short tick, a muted label and a muted 0 when empty. A sample record shows
its route (received, disposition, in a batch, results entered, reviewed, reported) on one line:
done stops filled, the current stop ringed with a soft halo, pending stops muted. The sidebar's
group stubs use the same colours. These dots, rings, ticks and stubs are shapes of the diagram,
not icons: they are the one exception to the icon rule in section 1.

**Retained devices.** Record foot line: the record's identifier, and when and by whom the view
was produced, under the last panel and on its printout; it is a viewing note, not a statement
about the record. Application footer (`includes/source_footer.html`): product name, the "Source
code" link (`SOURCE_CODE_URL`, required by AGPL-3.0 section 13) and the licence, on every staff,
portal, entrance, error and admin page; hidden only in form dialogs and on printouts. Struck prior
values: in history the value before a change is struck through in red pencil, the new value
follows. Sign-in: a panel on the sage ground beside the illustration; announcements for the
sign-in page sit beside the panel from 1280px. The retired notebook devices (quad ruling, red
margin rule, highlighter, heading band, numbered panel headings, monospace small-capital labels)
are not used.

Dates and times are rendered by the server in the laboratory's time zone (`TIME_ZONE`), never the
browser's.

**Dark mode** is forest night, composed rather than inverted: a deep green ground, the shell
darker than the panels, pale green for action and the current item.

**Print** always uses one light, high-contrast palette whatever the screen mode: white ground and
panels, panels edged with a hairline instead of a shadow, pills printed as icon and text on white,
no navigation, toolbars or buttons.

**Rules.** Text and its background meet WCAG 2.2 AA (4.5:1, labels and pills included) and field
outlines, checkbox edges, focus rings, the current nav fill and the line colours 3:1 against every
surface they sit on, in both modes and in print. Focus is always visible: the green ring and halo
on fields, a 2px outline elsewhere. Status never relies on colour alone. Motion is limited to
short colour transitions, and stops under `prefers-reduced-motion`; the sign-in scene is still.
Templates never carry colours: a new need is a token or a component rule in `app.css`. Chart.js
reads the tokens and the font, so charts follow the mode.
