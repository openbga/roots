import pytest
from django.contrib.auth.models import Group

from accounts.models import User


@pytest.fixture
def make_user(db):
    def make(name, *roles, **extra):
        user = User.objects.create_user(
            name, f"{name}@example.invalid", "test-password-729!", **extra
        )
        for role in roles:
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    return make


@pytest.fixture
def admin_user(make_user):
    return make_user("import-admin", "admin")


@pytest.fixture
def manager(make_user):
    return make_user("import-manager", "project_manager")


@pytest.fixture
def plain_static(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
