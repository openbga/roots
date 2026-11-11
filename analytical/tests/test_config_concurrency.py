"""An administrator's older configuration form must not erase a later edit."""

import pytest
from django.urls import reverse

from analytical.models import QCLimit
from core.models import AuditEvent

pytestmark = pytest.mark.django_db


def test_qc_limit_stale_form_and_fresh_retry(client, config, admin_user):
    client.force_login(admin_user)
    limit = QCLimit.objects.filter(definition=config["defs"]["Lead"], qc_type="lcs").get()
    url = reverse("analytical:config-edit", args=["qc-limit", limit.pk])
    form = client.get(url).context["form"]
    data = {f.name: f.value() if f.value() is not None else "" for f in form}
    data.update(lower="65", reason="Pending SOP edit")
    QCLimit.objects.filter(pk=limit.pk).update(lower="75")
    count = AuditEvent.objects.count()
    response = client.post(url, data)
    assert response.status_code == 200
    assert "changed after you opened" in str(response.context["form"].errors)
    assert response.context["form"]["lower"].value() == "65"
    limit.refresh_from_db()
    assert limit.lower == 75
    assert AuditEvent.objects.count() == count
    data["original_state"] = client.get(url).context["form"]["original_state"].value()
    assert client.post(url, data).status_code == 302
    limit.refresh_from_db()
    assert limit.lower == 65
    event = AuditEvent.objects.get(action="config.qclimit.updated")
    assert event.before["lower"] == "75.0000000000"
