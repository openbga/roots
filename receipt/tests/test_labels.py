from xml.etree import ElementTree

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from receipt.labels import barcode_svg, render_label_pdf, sample_labels

pytestmark = pytest.mark.django_db


def test_identity_labels_encode_stable_ids_without_receipt_mutation(intake):
    sample = intake["sample"]()
    container = sample.containers.get()
    labels = sample_labels([sample], copies=2)
    assert len(labels) == 2 and labels[0].code == f"C-{container.pk:08d}"
    assert labels[0].heading == container.label
    assert sample_labels([sample], kind="samples")[0].code == sample.lab_id
    svg = ElementTree.fromstring(barcode_svg(labels[0].code))
    assert svg.tag.endswith("svg") and len(list(svg.iter())) > 20
    sample.refresh_from_db()
    assert sample.revision == 1 and sample.custody_events.count() == 1
    for kwargs in [{"copies": 0}, {"copies": 11}, {"kind": "planned"}]:
        with pytest.raises(ValidationError):
            sample_labels([sample], **kwargs)
    with pytest.raises(ValidationError, match="No received"):
        sample_labels([])
    with pytest.raises(ValidationError, match="200 labels"):
        sample_labels([sample] * 21, copies=10)


def test_real_label_pdf_has_one_fixed_size_page_per_label_with_long_client_id(intake):
    from weasyprint import HTML

    sample = intake["sample"](client_sample_id="LONG-FIELD-IDENTIFIER-" + "X" * 98)
    labels = sample_labels([sample], copies=2)
    pdf = render_label_pdf(labels)
    assert pdf.startswith(b"%PDF-")
    from django.template.loader import render_to_string

    document = HTML(
        string=render_to_string(
            "receipt/pdf/labels.html",
            {
                "rows": [{"label": item, "barcode": barcode_svg(item.code)} for item in labels],
            },
        )
    ).render()
    assert len(document.pages) == 2
    assert all(page.width == 384 and page.height == 192 for page in document.pages)


def test_download_checks_scope_options_and_returns_native_pdf(client, intake):
    sample = intake["sample"]()
    url = reverse("receipt:sample-labels", args=[sample.pk])
    assert client.get(url).status_code == 302
    client.force_login(intake["user"]("portal", client=intake["client"]))
    assert client.get(url).status_code == 403
    client.force_login(intake["actor"])
    assert client.get(url, {"copies": "bad"}).status_code == 400
    response = client.get(url)
    assert response.status_code == 200 and response.content.startswith(b"%PDF-")
    assert response["Content-Type"] == "application/pdf"
    assert response["Cache-Control"] == "private, no-store"
