import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from commercial.models import PriceList, Quote, Submission
from core.models import Lab


@pytest.mark.django_db
@pytest.mark.parametrize("lab_name", [None, "Riverside Water Laboratory"])
def test_commercial_seed_refuses_a_non_demo_laboratory(lab, lab_name):
    """Accounts that happen to be named demo_* are not enough to write synthetic records."""
    from .conftest import make_user

    Lab.objects.filter(pk=1).delete()
    if lab_name:
        Lab.objects.create(pk=1, name=lab_name)
    make_user("demo_project_manager", "project_manager")
    make_user("demo_client", client=lab["a"]["client"])

    with pytest.raises(CommandError, match="non-demo laboratory"):
        call_command("seed_commercial_demo", verbosity=0)

    assert not PriceList.objects.exists()
    assert not Quote.objects.exists()
    assert not Submission.objects.exists()


@pytest.mark.django_db
def test_commercial_seed_runs_on_the_demo_laboratory(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-test-password-only")
    call_command("seed_demo", verbosity=0)
    call_command("seed_commercial_demo", verbosity=0)
    assert Quote.objects.count() == 3
    assert Submission.objects.count() == 3
