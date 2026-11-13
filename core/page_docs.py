"""Trusted, versioned operator help selected by the current authorized page's route.

Content is plain text in docs/pages/*.json (including subdirectories), never user
input or an independently accessible record endpoint. A match narrows a shared route
by its named URL arguments, such as a correction section or configuration kind.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

CONTENT_ROOT = Path(__file__).resolve().parent.parent / "docs" / "pages"

# The friendly layer (summary, common tasks, good to know): optional, but all or none. Limits are
# the writing guide's (.agents/skills/document-page); [[Label]] marks an exact on-screen label.
FRIENDLY_FIELDS = ("summary", "tasks", "good_to_know")
SUMMARY_MAX = 240
TASKS = (2, 5)
TASK_TITLE_MAX = 80
STEPS = (1, 4)
STEP_MAX = 200
TIPS = (3, 5)
TIP_MAX = 200
LABEL = re.compile(r"\[\[([^\[\]]+)\]\]")


def label_problem(text):
    """Why text's [[Label]] markers are malformed, or None: no nesting, every [[ closed."""
    rest = LABEL.sub("", text)
    if "[[" in rest or "]]" in rest:
        return "a [[ needs its closing ]] (labels cannot nest)"
    return None


def _check_text(path, where, value, limit):
    if not isinstance(value, str) or not value.strip():
        raise ImproperlyConfigured(f"{path}: {where} must be non-blank text")
    if len(value) > limit:
        raise ImproperlyConfigured(f"{path}: {where} is over {limit} characters")
    problem = label_problem(value)
    if problem:
        raise ImproperlyConfigured(f"{path}: {where}: {problem}")


def check_friendly(path, document):
    """The summary / tasks / good-to-know layer: all three or none, within the guide's limits."""
    present = [key for key in FRIENDLY_FIELDS if key in document]
    if not present:
        return
    if len(present) != len(FRIENDLY_FIELDS):
        missing = ", ".join(key for key in FRIENDLY_FIELDS if key not in document)
        raise ImproperlyConfigured(
            f"{path}: summary, tasks and good_to_know come together; missing {missing}"
        )
    summary = document["summary"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > SUMMARY_MAX:
        raise ImproperlyConfigured(f"{path}: summary must be 1-{SUMMARY_MAX} characters")
    if "[[" in summary or "]]" in summary:
        raise ImproperlyConfigured(f"{path}: summary is plain text, without [[labels]]")
    tasks = document["tasks"]
    if not isinstance(tasks, list) or not TASKS[0] <= len(tasks) <= TASKS[1]:
        raise ImproperlyConfigured(f"{path}: tasks needs {TASKS[0]}-{TASKS[1]} items")
    for number, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            raise ImproperlyConfigured(f"{path}: task {number} must be an object")
        title = task.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > TASK_TITLE_MAX:
            raise ImproperlyConfigured(
                f"{path}: task {number} needs a title of 1-{TASK_TITLE_MAX} characters"
            )
        steps = task.get("steps")
        if not isinstance(steps, list) or not STEPS[0] <= len(steps) <= STEPS[1]:
            raise ImproperlyConfigured(f"{path}: task {number} needs {STEPS[0]}-{STEPS[1]} steps")
        for index, step in enumerate(steps, 1):
            _check_text(path, f"task {number} step {index}", step, STEP_MAX)
    tips = document["good_to_know"]
    if not isinstance(tips, list) or not TIPS[0] <= len(tips) <= TIPS[1]:
        raise ImproperlyConfigured(f"{path}: good_to_know needs {TIPS[0]}-{TIPS[1]} items")
    for index, tip in enumerate(tips, 1):
        _check_text(path, f"good_to_know item {index}", tip, TIP_MAX)


def read_documents():
    documents = []
    ids = set()
    mappings = set()
    for path in sorted(CONTENT_ROOT.rglob("*.json")):
        document = json.loads(path.read_text())
        for key in ("id", "title", "purpose"):
            if not isinstance(document.get(key), str) or not document[key].strip():
                raise ImproperlyConfigured(f"{path}: missing documentation {key}")
        if document["id"] in ids:
            raise ImproperlyConfigured(f"Duplicate documentation ID: {document['id']}")
        ids.add(document["id"])
        if not isinstance(document.get("routes"), list) or not document["routes"]:
            raise ImproperlyConfigured(f"{path}: documentation needs routes")
        match = document.get("match", {})
        if not isinstance(match, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in match.items()
        ):
            raise ImproperlyConfigured(f"{path}: match must contain URL argument strings")
        for route in document["routes"]:
            key = (route, tuple(sorted(match.items())))
            if key in mappings:
                raise ImproperlyConfigured(f"Duplicate documentation route: {key}")
            mappings.add(key)
        if not isinstance(document.get("sections"), list) or not document["sections"]:
            raise ImproperlyConfigured(f"{path}: documentation needs sections")
        for section in document["sections"]:
            # Django's dot lookup otherwise resolves a missing "items" key to dict.items().
            section.setdefault("items", [])
            section.setdefault("paragraphs", [])
            if not section.get("title") or not (section.get("paragraphs") or section.get("items")):
                raise ImproperlyConfigured(f"{path}: empty documentation section")
            if not all(isinstance(value, str) for value in section.get("paragraphs", [])):
                raise ImproperlyConfigured(f"{path}: paragraphs must be plain text")
            for item in section.get("items", []):
                if not all(
                    isinstance(item.get(k), str) and item[k] for k in ("label", "description")
                ):
                    raise ImproperlyConfigured(f"{path}: each item needs a label and description")
        check_friendly(path, document)
        if not document.get("sources"):
            raise ImproperlyConfigured(f"{path}: documentation needs source evidence")
        documents.append(document)
    return tuple(documents)


@lru_cache(maxsize=1)
def _cached_documents():
    return read_documents()


def documents():
    # Authors see JSON edits on refresh; production reads the release's files once.
    return read_documents() if settings.DEBUG else _cached_documents()


def document_for_route(route, kwargs=None):
    kwargs = kwargs or {}
    candidates = (
        doc
        for doc in documents()
        if route in doc["routes"]
        and all(str(kwargs.get(key, "")) == value for key, value in doc.get("match", {}).items())
    )
    return max(candidates, key=lambda doc: len(doc.get("match", {})), default=None)


def documentation_for_route(route, kwargs=None):
    page = document_for_route(route, kwargs)
    if page is None:
        page = document_for_route("documentation:unavailable")
    if page is None:
        return None
    shared = document_for_route("documentation:shared")
    sections = list(page["sections"])
    if shared and page["id"] != shared["id"]:
        sections.extend(shared["sections"])
    # Source evidence and route metadata are authoring data, not operator content.
    fields = ("id", "title", "purpose", *FRIENDLY_FIELDS)
    return {key: page[key] for key in fields if key in page} | {"sections": sections}


def documentation_for_request(request):
    match = getattr(request, "resolver_match", None)
    return documentation_for_route(match.view_name if match else "", match.kwargs if match else {})
