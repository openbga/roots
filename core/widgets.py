"""Form controls for controlled vocabularies that still accept a value outside the list.

`ListOrOtherField` stores plain text: a listed label, or whatever the user typed after choosing
"Other". The model column stays a CharField, so values recorded before a list existed remain
valid and display unchanged. `SuggestInput` is a text box with a suggestion list.
"""

from django import forms

OTHER = "__other__"


class ListOrOtherWidget(forms.Select):
    """A select whose last option, "Other", reveals a text input (pure CSS, no script).

    The select keeps the field's own name, so a plain `name=value` submission is accepted as
    the value; `name=__other__` reads the text from `<name>_other` instead.
    """

    template_name = "django/forms/widgets/list_or_other.html"
    other_maxlength = None

    def value_from_datadict(self, data, files, name):
        value = data.get(name)
        if value == OTHER:
            return (data.get(f"{name}_other") or "").strip() or OTHER
        return value

    def get_context(self, name, value, attrs):
        listed = {str(choice) for choice, _ in self.choices}
        other = ""
        if value and str(value) not in listed:
            other, value = str(value), OTHER
        context = super().get_context(name, value, attrs)
        widget = context["widget"]
        widget["other"] = {
            "name": f"{name}_other",
            "id": f"{widget['attrs'].get('id', name)}_other",
            "value": other,
            "maxlength": self.other_maxlength,
        }
        return context


class ListOrOtherField(forms.CharField):
    """Text field entered from a list, with "Other" for a value the list does not have."""

    widget = ListOrOtherWidget

    def __init__(self, options, *, empty_label=None, **kwargs):
        super().__init__(**kwargs)
        if empty_label is None:
            empty_label = "Select…" if self.required else "Not recorded"
        self.widget.choices = [
            ("", empty_label),
            *((option, option) for option in options),
            (OTHER, "Other…"),
        ]
        self.widget.other_maxlength = self.max_length

    def widget_attrs(self, widget):
        attrs = super().widget_attrs(widget)
        attrs.pop("maxlength", None)  # belongs to the "Other" text input, not the select
        return attrs

    def clean(self, value):
        if value == OTHER:
            raise forms.ValidationError('Describe the value after choosing "Other".')
        return super().clean(value)


class SuggestInput(forms.TextInput):
    """Free text with suggestions. `suggestions` is a list or a callable evaluated at render.

    Each suggestion is a value, or a (value, label) pair whose label describes the value in the
    menu (a qualifier code and its meaning); choosing it still enters only the value.
    """

    template_name = "django/forms/widgets/suggest_input.html"

    def __init__(self, attrs=None, suggestions=()):
        super().__init__({"autocomplete": "off", **(attrs or {})})
        self.suggestions = suggestions

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        widget = context["widget"]
        values = self.suggestions() if callable(self.suggestions) else self.suggestions
        suggestions = {}
        for item in values:
            value, label = item if isinstance(item, (tuple, list)) else (item, "")
            if value:
                suggestions.setdefault(value, label)  # the first label of a repeated value
        widget["suggestions"] = list(suggestions.items())
        if widget["suggestions"]:
            widget["attrs"]["list"] = f"{widget['attrs'].get('id', name)}_list"
        return context
