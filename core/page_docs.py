"""Trusted, versioned operator help selected by the current authorized page's route.

Content is plain text in docs/pages/*.json (including subdirectories), never user
input or an independently accessible record endpoint. A match narrows a shared route
by its named URL arguments, such as a correction section or configuration kind.
"""

import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

CONTENT_ROOT = Path(__file__).resolve().parent.parent / "docs" / "pages"


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
    return {key: page[key] for key in ("id", "title", "purpose")} | {"sections": sections}


def documentation_for_request(request):
    match = getattr(request, "resolver_match", None)
    return documentation_for_route(match.view_name if match else "", match.kwargs if match else {})
