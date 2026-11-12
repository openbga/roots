from django import forms
from django.utils import timezone

from .announcements import AUDIENCES, BODY_LIMIT, STATES
from .models import Announcement

DATETIME = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
AUDIENCE_CHOICES = [
    ("sign_in", "Sign-in page (public: anyone can read it before signing in)"),
    ("staff", "Staff dashboard"),
    ("clients", "Client portal home page"),
]
PAGE_SIZES = [(size, f"{size} rows") for size in (25, 50, 100)]


class AnnouncementForm(forms.ModelForm):
    """Post or change an announcement; core.announcements applies the same rules on save."""

    audiences = forms.MultipleChoiceField(
        label="Show on",
        choices=AUDIENCE_CHOICES,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        error_messages={"required": "Choose at least one place to show this announcement."},
        help_text="Each place shows the announcements that are current for it, newest first. "
        "Keep confidential and client information out of anything shown on the sign-in page.",
    )
    groups = (
        ("Announcement", ["title", "body", "tone"]),
        ("Where and when", ["audiences", "starts_at", "ends_at"]),
    )

    class Meta:
        model = Announcement
        fields = ["title", "body", "tone", "starts_at", "ends_at"]
        labels = {
            "body": "Message",
            "tone": "Importance",
            "starts_at": "Show from",
            "ends_at": "Show until",
        }
        help_texts = {
            "body": f"Plain text, at most {BODY_LIMIT} characters. Line breaks are kept; "
            "formatting and links are not.",
            "tone": "Important announcements are marked Important and use the warning colours.",
            "starts_at": "Laboratory local time. A later time schedules the announcement.",
            "ends_at": "Optional. Leave blank to show the announcement until it is withdrawn.",
        }
        widgets = {
            "body": forms.Textarea(attrs={"rows": 7}),
            "starts_at": DATETIME,
            "ends_at": DATETIME,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["audiences"] = [
                key for key, (field, _) in AUDIENCES.items() if getattr(self.instance, field)
            ]
        else:
            self.initial.setdefault("starts_at", timezone.now())

    def grouped(self):
        return [(legend, [self[name] for name in names]) for legend, names in self.groups]

    def clean(self):
        data = super().clean()
        starts, ends = data.get("starts_at"), data.get("ends_at")
        if starts and ends and ends <= starts:
            self.add_error("ends_at", "Show until must be later than Show from.")
        chosen = data.get("audiences") or []
        for key, (field, _) in AUDIENCES.items():
            setattr(self.instance, field, key in chosen)
        return data


class AnnouncementFilterForm(forms.Form):
    """List filters. Invalid input is reported and never widened."""

    q = forms.CharField(
        label="Search",
        required=False,
        max_length=200,
        widget=forms.SearchInput(attrs={"placeholder": "Title or message"}),
    )
    state = forms.ChoiceField(
        label="Status",
        required=False,
        choices=[("", "Any status"), *((key, label) for key, (label, _) in STATES.items())],
    )
    audience = forms.ChoiceField(
        label="Shown on",
        required=False,
        choices=[("", "Anywhere"), *((key, label) for key, (_, label) in AUDIENCES.items())],
    )
    per_page = forms.TypedChoiceField(
        label="Rows per page", choices=PAGE_SIZES, coerce=int, required=False
    )
    sort = forms.CharField(required=False, widget=forms.HiddenInput())
