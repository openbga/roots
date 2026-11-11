"""Mapped import pages: saved column mappings and the batch import that uses them.

Views translate requests. Mapping rules live in import_profiles, validation and the write in
importing, and the result entry rules in services."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from core.concurrency import protect_form

from . import forms as f
from . import import_profiles, importing, selectors, services
from .models import ImportProfile
from .views import (
    _batch,
    _cleaned,
    _filter_context,
    _grid,
    _paginate,
    _render,
    _report_errors,
    _scoped,
    _staff,
    import_tabs,
)

PREVIEW_ROWS = 100


@login_required
def profile_list(request):
    _staff(request)
    form = f.ImportProfileFilterForm(request.GET or None)
    data = _cleaned(form)
    grid = _grid(request, selectors.IMPORT_PROFILE_SORTS, "name")
    qs = selectors.filter_import_profiles(
        selectors.import_profile_queryset(),
        q=(data.get("q") or "").strip(),
        layout=data.get("layout", ""),
        state=data.get("state", ""),
    )
    qs = _scoped(form, qs).order_by(*selectors.ordering(selectors.IMPORT_PROFILE_SORTS, grid.sort))
    ctx = _filter_context(
        request,
        form,
        advanced=("state",),
        grid="analytical.import_profiles",
        placeholder="Profile name or note",
    )
    return _render(
        request,
        "import_profile_list.html",
        {
            "page_title": "Import mappings",
            **ctx,
            "page_obj": _paginate(grid, qs),
            "sort": grid.sort,
            "filtered": ctx["active"],
        },
    )


def _profile_form(request, *, profile=None):
    services.require_role(request.user, services.ANALYST)
    editing = profile is not None
    form = f.ImportProfileForm(
        request.POST or None,
        initial=f.ImportProfileForm.initial_for(profile) if editing else None,
        editing=editing,
    )
    protect_form(form, profile)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            saved = (
                import_profiles.update_profile(
                    actor=request.user,
                    profile_id=profile.pk,
                    name=data["name"],
                    layout=data["layout"],
                    delimiter=data["delimiter"],
                    mapping=form.mapping,
                    notes=data["notes"],
                    is_active=data["is_active"],
                    expected_state=data["original_state"],
                )
                if editing
                else import_profiles.create_profile(
                    actor=request.user,
                    name=data["name"],
                    layout=data["layout"],
                    delimiter=data["delimiter"],
                    mapping=form.mapping,
                    notes=data["notes"],
                )
            )
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(
                request,
                f"Saved the mapping profile '{saved.name}'. Preview a file with it before "
                "importing: a profile is your description of a file, not a validated adapter.",
            )
            return redirect("analytical:import-profile-list")
    return _render(
        request,
        "import_profile_form.html",
        {
            "page_title": f"Edit {profile.name}" if editing else "New mapping",
            "form": form,
            "profile": profile,
            "submit_label": "Save changes" if editing else "Create mapping",
            "date_formats": import_profiles.DATE_FORMATS,
        },
    )


@login_required
def profile_create(request):
    _staff(request)
    return _profile_form(request)


@login_required
def profile_edit(request, pk):
    _staff(request)
    return _profile_form(
        request, profile=get_object_or_404(selectors.import_profile_queryset(), pk=pk)
    )


def _form_errors_as_messages(request, form):
    """The confirm form has only hidden fields, so its errors are shown as messages."""
    for errors in form.errors.values():
        for error in errors:
            messages.error(request, f"{error} Nothing was imported.")


def _change(row):
    """What committing this row does, in words."""
    if row.previous is None:
        return "New result"
    return (
        f"Revision {row.previous.number + 1}; the earlier value "
        f"'{row.previous.supplied_text}' ({row.previous.get_status_display().lower()}) is kept"
    )


@login_required
def batch_import_mapped(request, pk):
    _staff(request)
    batch = _batch(pk)
    upload = f.MappedImportUploadForm(initial={"profile": request.GET.get("profile")})
    preview = commit_form = None
    if request.method == "POST":
        services.require_role(request.user, services.ANALYST)
        if request.POST.get("step") == "commit":
            commit_form = f.MappedImportCommitForm(request.POST)
            if commit_form.is_valid():
                d = commit_form.cleaned_data
                try:
                    revisions = importing.commit_profile_import(
                        actor=request.user,
                        batch_id=batch.pk,
                        profile_id=d["profile_id"],
                        data=d["payload"],
                        filename=d["filename"],
                        mapping_hash=d["mapping_hash"],
                        state_token=d["state_token"],
                    )
                except ValidationError as exc:
                    _report_errors(request, exc)
                    profile = ImportProfile.objects.filter(pk=d["profile_id"]).first()
                    if profile:
                        preview = importing.parse_profile_import(
                            batch, profile, d["payload"], filename=d["filename"]
                        )
                except ImportProfile.DoesNotExist:
                    messages.error(request, "That mapping profile no longer exists.")
                else:
                    messages.success(
                        request,
                        f"Imported {len(revisions)} result(s) from {d['filename']} as entered. "
                        "Review them, then submit for review.",
                    )
                    return redirect("analytical:batch-detail", pk=batch.pk)
            else:
                _form_errors_as_messages(request, commit_form)
        else:
            upload = f.MappedImportUploadForm(request.POST, request.FILES)
            if upload.is_valid():
                file, profile = upload.cleaned_data["file"], upload.cleaned_data["profile"]
                data = file.read(importing.MAX_BYTES + 1)
                preview = importing.parse_profile_import(
                    batch, profile, data, filename=file.name[:200]
                )
                if len(data) <= importing.MAX_BYTES:
                    commit_form = f.MappedImportCommitForm(
                        initial={
                            "filename": file.name[:200],
                            "payload": importing.encode_payload(data),
                            "profile_id": profile.pk,
                            "mapping_hash": preview.mapping_hash,
                            "state_token": preview.state_token,
                        }
                    )
    shown = None
    if preview is not None:
        for row in preview.rows:
            row.change = _change(row) if row.result is not None and not row.errors else ""
        bad = [r for r in preview.rows if r.errors]
        good = [r for r in preview.rows if not r.errors]
        shown = {
            "rows": bad + good[:PREVIEW_ROWS],
            "hidden_ok": max(len(good) - PREVIEW_ROWS, 0),
            "new": sum(1 for r in good if r.previous is None),
            "revisions": sum(1 for r in good if r.previous is not None),
            "blanks": preview.blanks[:50],
            "hidden_blanks": max(len(preview.blanks) - 50, 0),
        }
    return _render(
        request,
        "batch_import_mapped.html",
        {
            "page_title": "Import with a mapping",
            "section_tabs": import_tabs(batch, "analytical:batch-import-mapped"),
            "batch": batch,
            "upload": upload,
            "preview": preview,
            "shown": shown,
            "commit_form": commit_form,
            "profiles_exist": ImportProfile.objects.filter(is_active=True).exists(),
            "max_kib": importing.MAX_BYTES // 1024,
            "max_rows": importing.MAX_ROWS,
            "profile_url": reverse("analytical:import-profile-list"),
        },
    )
