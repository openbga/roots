"""Client record page: the one place a project manager sees a client's people, projects and work."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render

from accounts.models import User
from accounts.roles import require_role, require_staff
from core.models import AuditEvent

from .contacts import add_contact, update_contact
from .forms import ContactEditForm, ContactForm
from .models import Client, ClientContact, Order

RECENT = 8


@login_required
def client_detail(request, pk):
    require_staff(request.user)
    client = get_object_or_404(Client, pk=pk)
    orders = Order.objects.filter(project__client=client).select_related("project")
    return render(
        request,
        "receipt/client_detail.html",
        {
            "page_title": client.name,
            "client": client,
            "contacts": client.contacts.all(),
            "projects": client.projects.select_related("project_manager")
            .annotate(order_count=Count("orders"))
            .order_by("-is_active", "name"),
            "orders": orders.annotate(sample_count=Count("samples")).order_by("-created_at")[
                :RECENT
            ],
            "order_total": orders.count(),
            # Reverse accessors from the commercial app.
            "quotes": client.quotes.all()[:RECENT],
            "submissions": client.submissions.all()[:RECENT],
            "portal_users": User.objects.filter(client=client).order_by("-is_active", "email"),
            "history": AuditEvent.objects.filter(
                object_type="receipt.client", object_id=str(client.pk)
            )
            .select_related("actor")
            .order_by("-occurred_at")[:RECENT],
        },
    )


def _problem(form, exc):
    if hasattr(exc, "message_dict"):
        for name, errors in exc.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, exc)


@login_required
def contact_edit(request, pk, contact_pk=None):
    require_role(request.user, "project_manager")
    client = get_object_or_404(Client, pk=pk)
    contact = get_object_or_404(ClientContact, pk=contact_pk, client=client) if contact_pk else None
    if contact:
        form = ContactEditForm(
            request.POST or None, instance=contact, initial={"expected_revision": contact.revision}
        )
    else:
        form = ContactForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            if contact:
                update_contact(actor=request.user, contact_id=contact.pk, **form.cleaned_data)
            else:
                add_contact(actor=request.user, client_id=client.pk, **form.cleaned_data)
        except ValidationError as exc:
            _problem(form, exc)
        else:
            messages.success(request, "Contact saved.")
            return redirect("receipt:client-detail", pk=client.pk)
    return render(
        request,
        "receipt/contact_form.html",
        {
            "page_title": f"Edit {contact.name}" if contact else "Add contact",
            "client": client,
            "form": form,
            "contact": contact,
            "history": AuditEvent.objects.filter(
                object_type="receipt.clientcontact", object_id=str(contact.pk)
            )
            .select_related("actor")
            .order_by("-occurred_at")
            if contact
            else [],
        },
    )
