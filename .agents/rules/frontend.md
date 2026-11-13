---
paths:
  - "templates/**"
  - "*/templates/**"
  - "static/css/**"
  - "static/js/**"
  - "*/static/**"
  - "core/templatetags/**"
  - "*/forms.py"
  - "*/*_forms.py"
---

# Frontend rules

Full standard: `docs/ui-standards.md`. Enforced by `tests/test_ui_standards.py` and `tests/test_css_tokens.py`.

- Build pages only from the shared components: `{% load ui %}` block tags (`panel`, `list_results`, `notice`,
  `form_page`, `history_panel`, `fact`, `page_meta`, …) and `templates/includes/*.html`. Read the header comment of
  an include before using it. Never copy a component's markup into a page; ask for a parameter instead.
- Colours, spacing, type, radius, shadow and z-index come only from `--vr-*` tokens in the Tokens section of
  `static/css/app.css`. No hex/rgb/hsl, raw px/rem, inline `style` or untokened Bootstrap colour utilities.
- Icons are Bootstrap Icons only (pinned version in `static/vendor/README.md`), copied verbatim into
  `static/img/icons.svg` as `i-<name>`, rendered through `includes/icon.html` or `vrUi.icon`. Never draw or invent
  an SVG, glyph or CSS shape for an icon. Exceptions: the brand mark and the sign-in scene.
- Controls are Bootstrap form components through the Django widget templates in `templates/django/forms/widgets/`.
  Never render a browser-default control (native select menu, date/time popup, number spinner, datalist,
  validation bubble, title tooltip). Selects use the shared Tom Select component; large model lists use
  the remote select with a registered, permission-scoped lookup; dates use the shared date field.
- Validation is server-side in the Django form; errors render inline through the form components.
- When a page's visible content, actions or fields change, update its help (`.agents/skills/document-page`).
