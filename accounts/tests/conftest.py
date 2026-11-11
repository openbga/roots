import pytest
from django.contrib.auth.models import Group

from accounts.models import User

PASSWORD = "test-password-729!"


@pytest.fixture
def make_user(db):
    def make(name, *roles, **extra):
        user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD, **extra)
        for role in roles:
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    return make
