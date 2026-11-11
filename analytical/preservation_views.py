"""Staff preservation evidence and append-only assessment form."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from accounts.roles import has_role, require_role, require_staff
from receipt.models import RequestedTest

from .forms import add_service_errors
from .models import PreservationDecision
from .preservation_services import (
    DECISION_ROLES,
    preservation_history,
    preservation_summaries,
    record_preservation_decision,
)


class DecisionForm(forms.Form):
    expected_fingerprint = forms.CharField(widget=forms.HiddenInput)
    expected_decision_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    decision = forms.ChoiceField(
        choices=PreservationDecision.Decision.choices, label="Lab disposition"
    )
    reason = forms.CharField(
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 4}),
        label="Assessment evidence and reason",
        help_text=(
            "Address findings and manual requirements. A disposition does not establish "
            "regulatory validity or an exception. Proceed/Qualify evidence and reasons appear "
            "on issued PDFs and client copies; avoid internal-only notes here."
        ),
    )


@login_required
def assess(request, pk):
    require_staff(request.user)
    if request.method == "POST":
        require_role(request.user, *DECISION_ROLES)
    test = get_object_or_404(
        RequestedTest.objects.select_related("sample", "definition__method", "definition__analyte"),
        pk=pk,
    )
    summary = preservation_summaries(test.sample).get(test.pk)
    if summary is None:
        raise Http404("This test is no longer requested.")
    displayed_decision = summary.blocking_decision or summary.latest_decision
    form = DecisionForm(
        request.POST or None,
        initial={
            "expected_fingerprint": summary.fingerprint,
            "expected_decision_id": summary.latest_decision.pk if summary.latest_decision else None,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            record_preservation_decision(
                actor=request.user, requested_test_id=pk, **form.cleaned_data
            )
        except ValidationError as exc:
            add_service_errors(form, exc)
        else:
            messages.success(
                request, "Preservation assessment recorded. Earlier evidence is retained."
            )
            return redirect("analytical:preservation-assess", pk=pk)
    return render(
        request,
        "analytical/preservation_assess.html",
        {
            "page_title": "Preservation assessment",
            "test": test,
            "summary": summary,
            "displayed_decision": displayed_decision,
            "decision_tone": {
                "hold": "hold",
                "reject": "danger",
                "qualify": "warning",
                "proceed": "neutral",
            }.get(displayed_decision.decision if displayed_decision else "", "neutral"),
            "evidence": summary.evidence,
            "form": form,
            "can_assess": has_role(request.user, *DECISION_ROLES),
            "history": list(reversed(preservation_history(test.sample))),
        },
    )
