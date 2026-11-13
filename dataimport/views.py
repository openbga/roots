"""Spreadsheet import pages: transport only. importing.py holds the columns, preview and commit.

Every page requires a staff role, and each record type the role of its owning service; the
services check the same roles again on every row.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render

from accounts.roles import require_role, require_staff
from analytical.importing import encode_payload

from . import importing, starter
from .forms import CommitForm, UploadForm


def _record_type(request, kind):
    require_staff(request.user)
    record_type = importing.BY_SLUG.get(kind)
    if record_type is None:
        raise Http404("No such record type.")
    require_role(request.user, *record_type.roles)
    return record_type


def _csv(data, filename):
    response = HttpResponse(data, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


def import_title(record_type):
    title = record_type.title
    return "Import " + (title if title.startswith("QC") else title[0].lower() + title[1:])


@login_required
def index(request):
    require_staff(request.user)
    types = importing.allowed_types(request.user)
    if not types:
        raise PermissionDenied("Your role does not permit importing setup records.")
    allowed = {record_type.slug for record_type in types}
    starters = [
        {**entry, "record_title": importing.BY_SLUG[entry["record_type"]].title}
        for entry in starter.files()
        if entry["record_type"] in allowed
    ]
    hidden_starters = len(starter.files()) - len(starters)
    return render(
        request,
        "dataimport/index.html",
        {
            "page_title": "Spreadsheet import",
            "types": [
                {"spec": record_type, "position": position, "title": import_title(record_type)}
                for position, record_type in enumerate(types, start=1)
            ],
            "all_types": len(types) == len(importing.TYPES),
            "starters": starters,
            "hidden_starters": hidden_starters,
            "starter_meta": starter.manifest(),
            "max_kib": importing.MAX_BYTES // 1024,
            "max_rows": importing.MAX_ROWS,
        },
    )


@login_required
def template(request, kind):
    record_type = _record_type(request, kind)
    return _csv(importing.template_bytes(record_type), f"{record_type.slug}-template.csv")


@login_required
def starter_download(request, name):
    require_staff(request.user)
    entry = starter.entry(name)
    if entry is None:
        raise Http404("No such starter file.")
    require_role(request.user, *importing.BY_SLUG[entry["record_type"]].roles)
    # A byte-order mark tells spreadsheet programs the file is UTF-8 (°C, µg/L).
    return _csv("﻿".encode() + starter.read(name), name)


def _columns(record_type):
    out = []
    for column in record_type.columns:
        requirement, format_text = record_type.describe(column)
        out.append(
            {
                "name": column.name,
                "requirement": requirement,
                "format": format_text,
                "help": column.help,
            }
        )
    return out


@login_required
def upload(request, kind):
    record_type = _record_type(request, kind)
    form = UploadForm()
    preview = commit_form = None
    if request.method == "POST" and request.POST.get("step") == "commit":
        commit_form = CommitForm(request.POST)
        if commit_form.is_valid():
            cleaned = commit_form.cleaned_data
            try:
                done = importing.commit_file(
                    record_type,
                    actor=request.user,
                    data=cleaned["payload"],
                    filename=cleaned["filename"],
                    reason=cleaned["reason"],
                    state_token=cleaned["state_token"],
                )
            except ValidationError as exc:
                preview = importing.preview_file(
                    record_type,
                    actor=request.user,
                    data=cleaned["payload"],
                    filename=cleaned["filename"],
                )
                if hasattr(exc, "error_dict"):  # the reason is missing; the preview still holds
                    for name, errors in exc.message_dict.items():
                        for error in errors:
                            commit_form.add_error(
                                name if name in commit_form.fields else None, error
                            )
                else:
                    for message in exc.messages:
                        messages.error(request, message)
                    commit_form = CommitForm(
                        initial={
                            "filename": cleaned["filename"],
                            "payload": encode_payload(cleaned["payload"]),
                            "state_token": preview.state_token,
                            "reason": cleaned["reason"],
                        }
                    )
            else:
                counts = done.counts
                messages.success(
                    request,
                    f"Imported {done.filename}: {counts['create']} created, "
                    f"{counts['update']} changed, {counts['unchanged']} unchanged.",
                )
                return redirect(record_type.list_route)
        else:
            for errors in commit_form.errors.values():
                for error in errors:
                    messages.error(request, f"{error} Nothing was imported.")
            commit_form = None
    elif request.method == "POST":
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload_file = form.cleaned_data["file"]
            data = upload_file.read(importing.MAX_BYTES + 1)
            filename = upload_file.name[:200]
            if len(data) > importing.MAX_BYTES:
                form.add_error(
                    "file", f"The file is larger than {importing.MAX_BYTES // 1024} KiB."
                )
            else:
                preview = importing.preview_file(
                    record_type, actor=request.user, data=data, filename=filename
                )
                commit_form = CommitForm(
                    initial={
                        "filename": filename,
                        "payload": encode_payload(data),
                        "state_token": preview.state_token,
                    }
                )
    return render(
        request,
        "dataimport/upload.html",
        {
            "page_title": import_title(record_type),
            "spec": record_type,
            "form": form,
            "preview": preview,
            "commit_form": commit_form,
            "columns": _columns(record_type),
            "max_kib": importing.MAX_BYTES // 1024,
            "max_rows": importing.MAX_ROWS,
        },
    )
