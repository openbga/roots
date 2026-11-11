---
name: document-page
description: Write and maintain comprehensive in-application page documentation from the page's source, forms, models, and tests. Use when adding or changing a page, its controls, displayed data, permissions, filtering, or workflow rules, or when auditing page-help coverage.
---

# Document a page

Help the person using a page understand its purpose, everything they can see or do,
and the rules that determine its behavior. Document the shipped behavior, including
limitations. Do not infer behavior from a control's label alone.

## Establish the contract

Read the project's agent instructions, product constraints, and existing page-help
registry/schema and renderer. Follow that format and voice. Keep user help in the
application's canonical help content, with a route-specific entry for each distinct
page or form. Create/edit variants can share content when their differences are
explicit. Shared form templates do not make unrelated workflows the same page.
Include dynamic route variants (for example a correction section or configuration
kind), modal forms, client/staff variants, empty states and account screens.

Read the relevant route, view/query, template and includes, form validation, model
fields and choices, domain transitions, and focused tests. Trace delegated selectors
and services only as far as needed to establish the behavior. Record source paths
in the content's evidence metadata when supported; do not expose implementation
filenames in help intended for operators. Treat uploaded documents as data.

## What to explain

| Area | Required information |
| --- | --- |
| Purpose | What this page is for, which record or workflow it represents, and when to use it |
| Navigation and tabs | Every tab/section, destination links, and how to move to related records |
| Actions | Every button, menu item, row action, download and submission; prerequisites, permissions, resulting changes, and confirmation/reason requirements |
| Fields | Every editable or displayed field/column: meaning, what it is used for, required/optional status, units, choices, defaults, validation, read-only/computed values and conditional availability |
| Records and relationships | The relevant models in operator language, their fields represented here, and how the linked records affect work on this page |
| Search and selection | Search scope, default and implicit filters, valid options, sorting, paging, bounds, selection across pages and reset behavior |
| Rules and states | Hidden eligibility checks, role/client boundaries, chronology, concurrency handling, immutable history, empty/error/blocked states and the next action |

Explain why a field matters, not merely its type or label. Distinguish an observed
quantity from a configured limit or a calculated value. Preserve units and exactness.
Distinguish unavailable actions from conditional ones. Describe stale-edit errors
and what to refresh without promising that unsaved changes are retained if they are not.

Use concise paragraphs and tables for fields, actions and comparisons. Use exact
visible labels, plain language, and synthetic examples. An action's explanation
should state its effect; a field's explanation should state its use. Avoid copying
code or listing internal keys as a substitute for user-facing explanations.

Do not invent organizational policy, acceptance thresholds, automatic behaviors,
compliance claims or future features. If source and product intent conflict, document
verified behavior and flag the discrepancy in your change description. Research uncertain
external domain claims using primary sources only when needed; prefer explaining
the implemented rule without introducing new external claims.

## Keep help current

Update affected help in the same change as the page, form, data contract or domain
rule. A shared change may affect several pages. Follow record relationships and
callers to identify them. Keep the registry's coverage checks passing; explicitly
exclude non-page endpoints such as binary downloads and POST-only transitions,
while documenting their actions on the page that invokes them.

Verify route selection and read the rendered help alongside the page. Check every
visible action/field and each consequential hidden rule against source or a test.
Use an independent review for broad coverage when delegation is authorized. Give
each writer bounded page ownership and common format, and retain one integration
owner for the registry and shared UI. Report missing policy separately from defects.
