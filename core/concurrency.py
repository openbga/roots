"""Opaque original-state tokens for forms editing existing configuration records."""

import json

from django import forms
from django.core.exceptions import ValidationError
from django.utils.crypto import constant_time_compare, salted_hmac

from .audit import snapshot


def record_token(instance):
    payload = [instance._meta.label_lower, instance.pk, snapshot(instance)]
    return salted_hmac(
        "configuration.original_state", json.dumps(payload, sort_keys=True), algorithm="sha256"
    ).hexdigest()


def protect_form(form, instance):
    if instance and instance.pk:
        form.fields["original_state"] = forms.CharField(
            widget=forms.HiddenInput, initial=record_token(instance)
        )


def check_original_state(instance, expected):
    # Direct domain callers can omit a token; browser edit forms always require one.
    if expected is not None and (
        instance is None or not constant_time_compare(record_token(instance), expected)
    ):
        raise ValidationError(
            "This record changed after you opened it. Your input has been kept; "
            "reload and compare the current values before saving again."
        )
