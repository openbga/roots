"""Every form is built from the form components (docs/ui-standards.md section 7).

A page or action form is {% form_page %} (its local=True variant for an action inside a record
panel), whose shell, error summary, required note and action row come from the shared includes.
A page template may write its own <form> only for a different, named pattern, declared by a
{# form: <pattern> #} comment on the line before it:

  entry-grid      an entry grid (section 2): many rows of values in one table
  bulk-selection  a table's row checkboxes and the bulk action they feed
  filter          a GET form that only chooses what the page shows
  menu            a dropdown menu's GET form (the label download)
  button-target   an empty form that buttons elsewhere submit through form="..."

Shared includes (templates/includes/), allauth elements and Django form widgets own their markup.
"""

import re
from pathlib import Path

from django.apps import apps
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
PATTERNS = {"entry-grid", "bulk-selection", "filter", "menu", "button-target"}
OWNERS = ("templates/includes/", "templates/allauth/", "templates/django/")
MARKER = re.compile(r"\{#\s*form:\s*([a-z-]+)\b[^#]*#\}\s*$")


def page_templates():
    dirs = [Path(d) for d in settings.TEMPLATES[0]["DIRS"]]
    dirs += [Path(app.path) / "templates" for app in apps.get_app_configs()]
    for directory in dirs:
        if not directory.is_dir() or ROOT not in directory.parents or ".venv" in directory.parts:
            continue
        for path in sorted(directory.rglob("*.html")):
            name = path.relative_to(ROOT).as_posix()
            if not name.startswith(OWNERS):
                yield name, path.read_text()


def unnamed_forms():
    problems = []
    for name, source in page_templates():
        source = re.sub(
            r"{% comment %}.*?{% endcomment %}",
            lambda m: "\n" * m[0].count("\n"),
            source,
            flags=re.S,
        )
        lines = source.split("\n")
        for number, line in enumerate(lines):
            if not re.search(r"<form\b", line):
                continue
            before = line[: line.index("<form")]
            marker = MARKER.search(before) or (number and MARKER.search(lines[number - 1]))
            if not marker or marker.group(1) not in PATTERNS:
                problems.append(f"{name}:{number + 1}")
    return problems


def test_page_forms_use_the_form_components_or_name_their_pattern():
    problems = unnamed_forms()
    assert not problems, (
        "Build the form with {% form_page %} (local=True inside a record panel), or name its "
        "pattern on the line before with {# form: <pattern> #}, one of "
        + ", ".join(sorted(PATTERNS))
        + ":\n"
        + "\n".join(problems)
    )
