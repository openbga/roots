"""Shared form help treats ordinary strings as text in every widget branch."""

import pytest
from django import forms
from django.template.loader import render_to_string


@pytest.mark.parametrize(
    "field",
    [
        forms.CharField(),
        forms.BooleanField(),
        forms.MultipleChoiceField(choices=[("one", "One")], widget=forms.CheckboxSelectMultiple),
    ],
)
def test_help_text_is_escaped(field):
    field.help_text = '<script>alert("synthetic")</script>'
    form = forms.Form()
    form.fields["example"] = field
    html = render_to_string("includes/field.html", {"field": form["example"]})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
