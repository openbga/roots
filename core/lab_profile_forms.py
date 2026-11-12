"""Laboratory profile forms. core.lab_profile applies the same rules again when saving."""

from django import forms

from .lab_profile import (
    CERTIFICATE_NOTE_LIMIT,
    DEFAULT_CERTIFICATE_NOTE,
    IDENTITY_FIELDS,
    MAX_LOGO_SIDE,
    MIN_LOGO_SIDE,
)
from .models import MAX_LOGO_BYTES, Lab, LabCertificate

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class GroupedForm:
    groups = ()

    def grouped(self):
        return [(legend, [self[name] for name in names]) for legend, names in self.groups]


class LabIdentityForm(GroupedForm, forms.ModelForm):
    """Address, contact details and certificate statement printed on new reports."""

    groups = (
        ("Address", ["address_line1", "address_line2", "city", "state", "postal_code"]),
        ("Contact details", ["phone", "email", "website"]),
        ("Certificate statement", ["certificate_note"]),
    )

    class Meta:
        model = Lab
        fields = list(IDENTITY_FIELDS)
        labels = {"state": "State", "certificate_note": "Certificate statement"}
        help_texts = {
            "address_line1": "Optional. The laboratory's street address as reports print it.",
            "state": "Optional. US state or territory.",
            "phone": "Optional. As it should print, for example 555-0100.",
            "email": "Optional. An address clients can write to about reports.",
            "website": "Optional. The full address, starting with https:// or http://.",
            "certificate_note": (
                f"Optional, at most {CERTIFICATE_NOTE_LIMIT} characters. Printed under the "
                "certificate list on reports that list a certificate. Leave blank to print the "
                f"default statement: “{DEFAULT_CERTIFICATE_NOTE}”"
            ),
        }
        widgets = {"certificate_note": forms.Textarea(attrs={"rows": 4})}


class LabLogoForm(GroupedForm, forms.Form):
    """Upload the image printed at the top of new reports."""

    logo = forms.FileField(
        label="Logo image",
        widget=forms.FileInput(attrs={"accept": "image/png,image/jpeg"}),
        help_text=(
            f"A PNG or JPEG file of at most {MAX_LOGO_BYTES // 1024} KiB, each side between "
            f"{MIN_LOGO_SIDE} and {MAX_LOGO_SIDE} pixels, in RGB or greyscale colour. The file is "
            "kept exactly as uploaded."
        ),
    )
    groups = (("Logo", ["logo"]),)


class LabCertificateForm(GroupedForm, forms.ModelForm):
    """Add or change one certificate as the laboratory records it."""

    groups = (
        ("Certificate", ["authority", "program", "number"]),
        ("Validity", ["valid_from", "valid_until"]),
        ("On reports", ["show_on_reports", "position"]),
    )

    class Meta:
        model = LabCertificate
        fields = [
            "authority",
            "program",
            "number",
            "valid_from",
            "valid_until",
            "show_on_reports",
            "position",
        ]
        help_texts = {
            "authority": "The body that issued the certificate, as written on it.",
            "program": "Optional. The program, matrix or scope named on the certificate.",
            "number": "The certificate number or laboratory ID the authority assigned.",
            "valid_from": "Optional. Leave blank when the certificate states no start date.",
            "valid_until": "Optional. Leave blank when the certificate states no end date.",
            "show_on_reports": (
                "Print this certificate on reports issued within its validity dates. "
                "Outside them it is left off."
            ),
            "position": "Certificates are listed in this order, lowest first.",
        }
        widgets = {"valid_from": DATE, "valid_until": DATE}
