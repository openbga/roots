from django import forms

from analytical.forms import UploadPayloadField

from .importing import MAX_BYTES, MAX_ROWS


class UploadForm(forms.Form):
    file = forms.FileField(
        label="CSV file (UTF-8)",
        help_text=f"Save the sheet as CSV (UTF-8), comma separated, with the header row first. "
        f"Up to {MAX_BYTES // 1024} KiB and {MAX_ROWS} rows.",
    )


class CommitForm(forms.Form):
    filename = forms.CharField(widget=forms.HiddenInput, max_length=200)
    payload = UploadPayloadField()
    state_token = forms.CharField(widget=forms.HiddenInput, max_length=64)
    reason = forms.CharField(
        required=False,
        max_length=2000,
        label="Reason for changing existing records",
        help_text="Required because this file changes existing records. Recorded on every "
        "changed record together with the file name and its SHA-256.",
        widget=forms.Textarea(attrs={"rows": 2}),
    )
