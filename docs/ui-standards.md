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
  columns, Tom Select enhances the requested-tests picker, Chart.js draws control charts. Nothing
  else, and no self-written replacement for something these provide.
- One job, one component. Each concept in section 8 has exactly one shared include or one markup
  pattern. A page that needs a variation asks for a parameter on the shared part; it does not
  copy the markup.
- Templates carry no colours, no inline `style`, and no Bootstrap colour utilities that bypass
  the tokens (`bg-white`, `bg-light`, `text-dark`, `text-black`, `table-light`).
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

1. The sidebar owns destinations. Every list and overview a user can go to is a sidebar item.
   A destination appears in exactly one navigation component: never in the sidebar and again as
   tabs, pills or header buttons.
2. Section tabs exist for one case only: one sidebar item whose content is two to six sibling
   lists that are not themselves sidebar items (Catalog: Tests, Methods, Analytes; Inventory:
   Lots, Materials), or sibling sub-views of one action (the sections of a sample correction).
   They are links with their own URLs, rendered by `includes/section_tabs.html` directly under
   the page header, with `aria-current="page"` and no tab roles. One visual style. Tabs stay
   visible on every list in the set and never appear on record or form pages.
3. A long page with four or more sections gets an "On this page" list of anchors
   (`includes/page_toc.html`). It is not a tab set and does not hide content.
4. Every page marks exactly one sidebar item as current, including record, form and action pages
   beneath a list. Pages reached from the user menu (password, two-factor) mark none. The mapping
   from route to sidebar item is declared in one place and checked by a test (section 11). A
   route shared by two kinds of record (QC pages of an analytical or a preparation batch) is
   declared as shared, and the view names the item for the record at hand.
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
2. Columns. Every grid has a `Columns` button, on staff and portal pages alike. Identifier first
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
5. A side column (from 1200px) may hold the context and launch button for the most frequent
   record action. Its editing fields open in the shared form dialog. No panel inside a panel.
6. `Edit` is a secondary header button for records that can be edited freely. A locked record
   is changed through `Correct <thing>`, which requires a reason and is recorded in history.

## 7. Forms

House decision, approved 2026-11-10: use one Bootstrap modal system for editing, including
record-local action forms. A drawer would split equivalent workflows into two interaction
patterns. The record or list stays behind the dialog; long forms and entry grids can be maximized.
Small screens use the full screen. Normal dialogs have comfortable outer and inner margins;
the heading and close controls remain visible while the body scrolls. Controls use the Soft
paper treatment: lightly tinted fields, muted boundaries, modest rounding and quiet shadows,
while preserving graph paper, the red margin and the menu marker.

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
| Status of a record | `includes/status.html` (text, glyph, tone); the mapping from a domain state to one of seven tones lives in one small partial per app |
| Notice inside a page | Bootstrap `.alert` in the tone's variant, opening with `includes/status.html` in its `plain` form; `role="alert"` when it blocks the task, otherwise `role="status"` |
| Result of the last action | Django message at the top of the next page (`includes/messages.html`); it stays until dismissed or the next navigation. No toasts |
| Breadcrumbs | `includes/breadcrumbs.html` |
| Section tabs | `includes/section_tabs.html` |
| On-this-page anchors | `includes/page_toc.html` |
| List toolbar, chips, columns | `includes/grid_toolbar.html`, `filter_chips.html`, `grid_columns_button.html` |
| Sortable header | `includes/sort_header.html` |
| Pagination and count | `includes/pagination.html` |
| Empty, no results, filter error | `includes/empty_state.html` |
| Row selection | `includes/select_all.html` |
| Facts | `dl.vr-facts` |
| History | `includes/history_item.html` |
| Form error summary | `includes/form_errors.html` |
| Disclosure of filters | Bootstrap collapse, through the toolbar include |
| Disclosure of a note or long value | `<details>` |
| Menu of actions | Bootstrap dropdown |
| Editing or an action requiring input | Shared maximizable Bootstrap form dialog, with the object named |
| Confirmation of a destructive action | Compact Bootstrap confirmation dialog for a single yes or no; shared form dialog when input is required |
| Brand mark and name | `includes/brand.html` |
| Icon | `includes/icon.html` |

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
  links, more than one primary button in a page header.
- `tests/test_navigation.py` resolves every HTML route and asserts that it marks exactly one
  sidebar or portal item (or is declared as an account page), and that the item's label equals
  the list page's title.
- The browser suite keeps one journey per page type and the contrast check for both themes.

## 12. Visual language

The "field notebook" theme, approved 2026-11-09. `static/css/app.css` holds the one definition of
each token and component for light, dark and print; this section states the intent and the rules.

**Palette roles.** Each colour is a writing tool with one job; no colour is used outside its role.

| Role | Tokens | Used for |
| --- | --- | --- |
| Paper | `--vr-paper`, `--vr-paper-rule`, `--vr-paper-haze*`, `--vr-surface`, `--vr-surface-2`, `--vr-band`, `--vr-border`, `--vr-border-strong`, `--vr-topbar-*` | The sheet and its hazed ruling; opaque panels laid on it; the sage heading band of a panel, which the top bar shares (as the bgasoft.com hero does); hairlines; control boundaries |
| Ink (pine green) | `--vr-text`, `--vr-text-muted`, `--vr-ink`, `--vr-on-ink` | Text; the primary button |
| Pen (river teal) | `--vr-pen`, `--vr-pen-hover`, `--vr-focus` | Links, section numbers and the focus ring, nothing else |
| Highlighter (sunrise yellow, amber at night) | `--vr-highlight`, `--vr-highlight-edge`, `--vr-highlight-wash`, `--vr-on-highlight` | Where you are and what is selected: current sidebar item, current section tab, active filter chips, current page in a pager, selected rows and chosen items |
| Red pencil | `--vr-margin`, `--vr-pencil` | The margin rule, the required-field mark, prior values struck through |
| Stamp tones | `--vr-{neutral,info,progress,success,warning,danger,hold}-{text,bg,border}` | Statuses and notices only |

**Type.** Two voices. The system sans for reading. A system monospace (no web font) in small
capitals with wide tracking for labels: table headers, fact labels, sidebar groups, breadcrumbs,
toolbar labels. The same monospace, in normal case, for data: identifiers, dates and times,
counts and right-aligned figures, so 0/O and 1/l stay distinct and digits line up.

**Shape.** Square corners (controls 2px, panels and dialogs 0). Hierarchy comes from type, rules
and space, not shadows; menus and dialogs carry a hard paper offset instead of a soft shadow.
Table headings sit on a full-weight ink rule, rows on hairlines.

**Devices, and when each is used.**

- Paper: the signed-in workspace is one sheet with a faint quad ruling under a sage top bar. The
  ruling is hazed (slightly blurred and faded) so headings and the rail read cleanly on it. Data is
  never written on the ruling: tables, forms and facts sit on opaque panels.
- Margin: the staff sidebar is the notebook margin; a red margin rule divides it from the content
  (on phones it runs down the page's left edge). The client portal has no margin.
- Highlighter: a chisel-cut marker stroke for the one current location (sidebar item, section
  tab), a wash for selection. Its heavier lower edge is the state indicator; `aria-current` or the
  checkbox carries the state for assistive technology.
- Stamp: a status is rendered by `includes/status.html` as a rectangular stamp (mono capitals,
  tone, glyph and text, never colour alone). The record's own status, first in the header's key
  facts, is a full-size stamp pressed slightly askew; table cells use the frameless form.
- Numbered sections: on a record page each panel heading carries 1.0, 2.0 ... in pen. A page is
  a record page when it renders `includes/record_foot.html` in `{% block record_foot %}`.
- Record foot line: the record's identifier, and when and by whom the view was produced, under the
  last panel and on its printout. It is a viewing note, not a statement about the record.
- Application footer (`includes/source_footer.html`): product name, the "Source code" link
  (`SOURCE_CODE_URL`, required by AGPL-3.0 section 13) and the licence, on every staff, portal,
  entrance, error and admin page. Keep it; it is hidden only in form dialogs and on printouts.
- Dateline: today's date and ISO week above an overview's title (`includes/dateline.html`).
- Struck prior values: in history (`includes/history_item.html`) the value before a change is
  struck through in red pencil, the new value follows.
- Sign-in: a punched notebook sheet on a mint desk beside the illustration.

Dates and times in these devices are rendered by the server in the laboratory's time zone
(`TIME_ZONE`), never the browser's.

**Dark mode** is a night shift, composed rather than inverted: deep pine paper and panels, a faint
ruling, the margin still red, pale ink for text and the primary button, amber highlighter, and a
top bar in the dark heading band with light text.

**Print** always uses the light paper palette whatever the screen mode, with no ruling, no margin
rule, no navigation, toolbars or buttons, and stamp edges strong enough for a monochrome printer.

**Rules.** Text and its background meet WCAG 2.2 AA (4.5:1; labels and stamps count as small
text) and control boundaries, focus rings and state indicators 3:1, in both modes and in print.
Focus is a 2px pen outline outside the control, clearly
visible on the ruling. Motion is limited to short colour transitions and the sign-in scene, and
stops under `prefers-reduced-motion`. Templates never carry colours: a new need is a token or a
component rule in `app.css`. Chart.js reads the tokens, so charts follow the mode.
