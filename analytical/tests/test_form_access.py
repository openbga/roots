"""A form page refuses a GET from a role whose POST would be refused, instead of showing a form
that cannot be submitted (reachable by direct URL)."""

import pytest
from django.urls import reverse

from analytical import services as svc
from analytical.models import QCLimit
from analytical.qc import BatchQC
from receipt.models import RequestedTest

from .conftest import make_user
from .test_mdl_workflow import SPIKES, fill, study_for

pytestmark = pytest.mark.django_db


@pytest.fixture
def pages(config, analyst):
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    tests = RequestedTest.objects.filter(sample=config["samples"][0])
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[t.pk for t in tests]
    )
    key = BatchQC.load(batch).issues(config["defs"]["Lead"])[0].key
    study = study_for(config, analyst)
    fill(study, analyst, SPIKES)
    observation = study.observations.first()
    limit = QCLimit.objects.first()
    return {
        "qc_action": reverse("analytical:qc-action", args=[batch.pk]) + f"?target={key}",
        "config_edit": reverse("analytical:config-edit", args=["qc-limit", limit.pk]),
        "config_add": reverse("analytical:config-new", args=["qualifier"]),
        "mdl_exclude": reverse("analytical:mdl-exclude", args=[observation.pk]),
    }


# The roles each page's service accepts; every other staff role is refused on GET and POST.
ALLOWED = {
    "qc_action": {"analyst", "reviewer", "admin"},
    "config_edit": {"admin"},
    "config_add": {"admin"},
    "mdl_exclude": {"analyst", "reviewer", "admin"},
}


@pytest.mark.parametrize("page", sorted(ALLOWED))
@pytest.mark.parametrize("role", ["custodian", "project_manager", "analyst", "reviewer", "admin"])
def test_form_pages_open_only_for_roles_that_may_submit_them(client, pages, page, role):
    client.force_login(make_user(f"{role}-form", role))
    get = client.get(pages[page])
    assert get.status_code == (200 if role in ALLOWED[page] else 403)
    if role not in ALLOWED[page]:
        assert client.post(pages[page], {}).status_code == 403


def test_batch_page_links_to_the_disposition_form_only_for_roles_that_may_use_it(
    client, config, analyst, pages
):
    from analytical.models import Batch

    url = reverse("analytical:batch-detail", args=[Batch.objects.get().pk])
    for role, shown in (("custodian", False), ("project_manager", False), ("analyst", True)):
        client.force_login(make_user(f"{role}-link", role))
        page = client.get(url)
        assert page.status_code == 200
        assert (b"/qc/action/?target=" in page.content) is shown, role
