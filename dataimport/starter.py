"""Starter files: editable CSV files in the import format, read from public sources.

`starter/manifest.json` lists each file with its record type, what it contains, its sources and
the date they were read, and caveats. The files are starting points for a laboratory to check and
edit; they are imported like any other file and nothing here is applied automatically.
"""

import json
from functools import lru_cache
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .importing import BY_SLUG, TYPES

ROOT = Path(__file__).resolve().parent / "starter"


@lru_cache(maxsize=1)
def manifest():
    path = ROOT / "manifest.json"
    if not path.is_file():
        return {"files": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data.get("files", []):
        if entry.get("record_type") not in BY_SLUG:
            raise ImproperlyConfigured(f"Starter file {entry.get('file')}: unknown record type")
        if not (ROOT / entry["file"]).is_file() or "/" in entry["file"]:
            raise ImproperlyConfigured(f"Starter file {entry['file']} is missing")
    return data


def files():
    """Manifest entries in import order (the order of the record types)."""
    order = {record_type.slug: index for index, record_type in enumerate(TYPES)}
    return sorted(manifest()["files"], key=lambda e: (order[e["record_type"]], e["file"]))


def entry(name):
    return next((e for e in manifest()["files"] if e["file"] == name), None)


def read(name):
    return (ROOT / name).read_bytes()
