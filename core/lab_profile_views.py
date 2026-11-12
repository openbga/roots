"""Laboratory profile pages: the profile every staff role reads, and the admin role's forms.

Who may change what, and every rule on a change, live in core.lab_profile; these views only
collect input, show the service's errors on the form and redirect to the profile.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.roles import require_role, require_staff

from . import lab_profile as service
from .concurrency import protect_form, record_token
from .lab_profile_forms import LabCertificateForm, LabIdentityForm, LabLogoForm
from .models import Lab, LabCertificate

CERTIFICATE_ACTIONS = (
    "lab.certificate_added",
    "lab.certificate_changed",
    "lab.certificate_removed",
)


def _lab():
    return Lab.objects.filter(pk=1).first()


def _service_errors(form, exc):
    """Show a service ValidationError on the form, under the field it names where it names one."""
    if hasattr(exc, "error_dict"):
        for name, errors in exc.error_dict.items():
            for error in errors:
                form.add_error(name if name in form.fields else None, error)
    else:
        for message in exc.messages:
            form.add_error(None, message)


def _saved(request, changed, message):
    if changed:
        messages.success(request, message)
    else:
        messages.info(request, "No changes to save.")
    return redirect("lab-profile")


def _history():
    events = list(service.history())
    for event in events:
        if event.action in CERTIFICATE_ACTIONS:
            values = event.after or event.before or {}
            event.detail = " ".join(
                str(values[key]) for key in ("authority", "number") if values.get(key)
            )
    return events


@login_required
def lab_profile(request):
    require_staff(request.user)
    lab = _lab()
    can_manage = lab is not None and service.can_manage(request.user)
    certificates = service.certificates_with_state(timezone.localdate()) if lab else []
    for row in certificates:
        row.state_label, row.state_tone = service.CERTIFICATE_STATES[row.state]
        row.withheld = row.show_on_reports and row.state != "current"
        row.label = f"{row.authority} {row.number}"
        row.token = record_token(row) if can_manage else ""
    return render(
        request,
        "core/lab_profile.html",
        {
            "page_title": "Laboratory profile",
            "lab": lab,
            "logo": service.logo_facts(lab),
            "certificates": certificates,
            "default_certificate_note": service.DEFAULT_CERTIFICATE_NOTE,
            "can_manage": can_manage,
            "lab_token": record_token(lab) if can_manage else "",
            "history": _history() if lab else [],
        },
    )


def _form_page(request, form, *, title, submit_label, multipart=False, notes=()):
    return render(
        request,
        "core/lab_profile_form.html",
        {
            "page_title": title,
            "form": form,
            "submit_label": submit_label,
            "multipart": multipart,
            "notes": notes,
        },
    )


@login_required
def lab_profile_edit(request):
    require_role(request.user, *service.PROFILE_ROLES)
    lab = _lab()
    form = None
    if lab is not None:
        form = LabIdentityForm(request.POST if request.method == "POST" else None, instance=lab)
        protect_form(form, lab)
        if request.method == "POST" and form.is_valid():
            try:
                _, changed = service.save_identity(
                    actor=request.user,
                    values={name: form.cleaned_data[name] for name in service.IDENTITY_FIELDS},
                    expected_state=form.cleaned_data.get("original_state"),
                )
            except ValidationError as exc:
                _service_errors(form, exc)
            else:
                return _saved(request, changed, "Laboratory profile saved.")
    return _form_page(
        request,
        form,
        title="Edit laboratory profile",
        submit_label="Save changes",
        notes=(
            "These details print on reports issued from now on. Issued reports keep what they "
            "printed. The laboratory name is set when the application is installed and is not "
            "changed here.",
        ),
    )


@login_required
def lab_logo_edit(request):
    require_role(request.user, *service.PROFILE_ROLES)
    lab = _lab()
    form = None
    if lab is not None:
        posted = request.method == "POST"
        form = LabLogoForm(request.POST if posted else None, request.FILES if posted else None)
        protect_form(form, lab)
        if posted and form.is_valid():
            try:
                _, changed = service.set_logo(
                    actor=request.user,
                    uploaded=form.cleaned_data["logo"],
                    expected_state=form.cleaned_data.get("original_state"),
                )
            except ValidationError as exc:
                _service_errors(form, exc)
            else:
                return _saved(request, changed, "Logo saved. New reports print it.")
    replacing = lab is not None and lab.logo_id is not None
    return _form_page(
        request,
        form,
        title="Replace logo" if replacing else "Upload logo",
        submit_label="Replace logo" if replacing else "Upload logo",
        multipart=True,
        notes=(
            "The logo prints on reports issued from now on. Issued reports keep the logo they "
            "printed.",
        ),
    )


@login_required
@require_POST
def lab_logo_remove(request):
    require_role(request.user, *service.PROFILE_ROLES)
    try:
        # The page's confirmation always sends the token; a request without one is refused.
        _, changed = service.remove_logo(
            actor=request.user, expected_state=request.POST.get("original_state", "")
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("lab-profile")
    return _saved(request, changed, "Logo removed. New reports print no logo.")


@login_required
@require_GET
def lab_logo_image(request, sha256):
    require_staff(request.user)
    found = service.logo_image(sha256)
    if found is None:
        raise Http404("No such logo.")
    data, content_type = found
    response = HttpResponse(data, content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"
    # Content-addressed: the bytes behind a hash never change.
    response["Cache-Control"] = "private, max-age=31536000, immutable"
    return response


@login_required
def lab_certificate_edit(request, pk=None):
    require_role(request.user, *service.PROFILE_ROLES)
    instance = get_object_or_404(LabCertificate, pk=pk) if pk else None
    lab = _lab()
    form = None
    if lab is not None:
        form = LabCertificateForm(
            request.POST if request.method == "POST" else None, instance=instance
        )
        protect_form(form, instance)
        if request.method == "POST" and form.is_valid():
            try:
                _, changed = service.save_certificate(
                    actor=request.user,
                    instance=form.save(commit=False),
                    expected_state=form.cleaned_data.get("original_state"),
                )
            except ValidationError as exc:
                _service_errors(form, exc)
            else:
                return _saved(
                    request, changed, "Certificate saved." if instance else "Certificate added."
                )
    return _form_page(
        request,
        form,
        title=f"Edit {instance}" if instance else "Add certificate",
        submit_label="Save changes" if instance else "Add certificate",
        notes=(
            "Record the certificate as issued. It is printed as recorded and results are not "
            "checked against it.",
        ),
    )


@login_required
@require_POST
def lab_certificate_remove(request, pk):
    require_role(request.user, *service.PROFILE_ROLES)
    try:
        removed = service.remove_certificate(
            actor=request.user, pk=pk, expected_state=request.POST.get("original_state", "")
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            f"Removed certificate {removed['authority']} {removed['number']}. "
            "New reports no longer list it.",
        )
    return redirect("lab-profile")
