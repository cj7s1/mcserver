"""Permission matrix: global roles, per-instance grants, owner, inactive users."""

from __future__ import annotations

import pytest
from rest_framework.exceptions import NotFound, PermissionDenied

from apps.accounts.models import InstanceAccess, Role
from apps.accounts.permissions import ALL_CODES, INSTANCE_CODES, OPERATOR_CODES, SYSTEM_CODES, validate_codes
from apps.core.permissions import get_instance_for, require, visible_instances

pytestmark = pytest.mark.django_db


@pytest.fixture
def two(make_instance):
    return make_instance(name="A"), make_instance(name="B")


def grant(user, instance, role):
    return InstanceAccess.objects.create(user=user, instance=instance, role=role)


def test_owner_has_everything(users, two):
    owner = users["owner"]
    a, _ = two
    assert owner.global_permissions() == ALL_CODES
    assert owner.instance_permissions(a.pk) == INSTANCE_CODES
    assert owner.has_panel_perm("system.users")
    assert owner.has_panel_perm("instance.delete", a)


def test_inactive_users_have_nothing(make_user, two, roles):
    a, _ = two
    owner = make_user("ex-owner", owner=True, active=False)
    admin = make_user("ex-admin", role="administrator", active=False)
    granted = make_user("ex-granted", active=False)
    grant(granted, a, roles["server_manager"])
    for user in (owner, admin, granted):
        assert user.global_permissions() == frozenset()
        assert user.instance_permissions(a.pk) == frozenset()
        assert not user.has_panel_perm("instance.view", a)
    assert visible_instances(owner).count() == 0


def test_administrator_role(users, two):
    admin = users["admin"]
    assert admin.global_permissions() == ALL_CODES
    assert admin.instance_permissions(two[0].pk) == INSTANCE_CODES


def test_global_operator_applies_to_every_instance(users, two):
    op = users["operator"]
    for inst in two:
        assert op.instance_permissions(inst.pk) == frozenset(OPERATOR_CODES)
        assert op.has_panel_perm("instance.control", inst)
        assert not op.has_panel_perm("instance.configure", inst)
        assert not op.has_panel_perm("instance.restore", inst)
    assert not op.has_panel_perm("system.users")
    assert not op.has_panel_perm("instance.create")


def test_viewer_role(users, two):
    viewer = users["viewer"]
    assert viewer.instance_permissions(two[0].pk) == {"instance.view", "instance.console"}
    assert not viewer.has_panel_perm("instance.command", two[0])


def test_no_role_has_nothing(users, two):
    nobody = users["norole"]
    assert nobody.global_permissions() == frozenset()
    assert nobody.instance_permissions(two[0].pk) == frozenset()
    assert visible_instances(nobody).count() == 0


def test_instance_grant_applies_only_to_that_instance(users, two, roles):
    nobody = users["norole"]
    a, b = two
    grant(nobody, a, roles["server_manager"])
    assert nobody.instance_permissions(a.pk) == INSTANCE_CODES - {"instance.access"}
    assert nobody.instance_permissions(b.pk) == frozenset()
    assert nobody.has_panel_perm("instance.restore", a)
    assert not nobody.has_panel_perm("instance.restore", b)
    # an instance grant never confers system permissions
    assert nobody.global_permissions() == frozenset()
    assert not nobody.has_panel_perm("instance.create")
    assert list(visible_instances(nobody)) == [a]


def test_grant_adds_to_global_role(users, two, roles):
    viewer = users["viewer"]
    a, b = two
    grant(viewer, a, roles["operator"])
    assert viewer.instance_permissions(a.pk) == frozenset(OPERATOR_CODES | {"instance.console", "instance.view"})
    assert viewer.instance_permissions(b.pk) == {"instance.view", "instance.console"}


def test_system_codes_in_a_granted_role_are_ignored(users, two):
    nobody = users["norole"]
    a, _ = two
    sneaky = Role.objects.create(key="sneaky", name="Sneaky", permissions=["system.users", "instance.view", "instance.control"])
    grant(nobody, a, sneaky)
    assert nobody.instance_permissions(a.pk) == {"instance.view", "instance.control"}
    assert not nobody.has_panel_perm("system.users")
    assert not nobody.has_panel_perm("system.users", a)


def test_grant_without_view_does_not_make_instance_visible(users, two):
    nobody = users["norole"]
    a, _ = two
    blind = Role.objects.create(key="blind", name="Blind", permissions=["instance.control"])
    grant(nobody, a, blind)
    assert visible_instances(nobody).count() == 0
    with pytest.raises(NotFound):
        get_instance_for(nobody, a.pk, "instance.control")


def test_unknown_codes_in_roles_are_ignored():
    role = Role(key="x", name="X", permissions=["instance.view", "made.up"])
    assert role.permission_set == {"instance.view"}


def test_get_instance_for_and_require(users, two, roles):
    nobody = users["norole"]
    a, b = two
    grant(nobody, a, roles["viewer"])
    assert get_instance_for(nobody, a.pk) == a
    with pytest.raises(PermissionDenied):
        get_instance_for(nobody, a.pk, "instance.command")
    with pytest.raises(NotFound):
        get_instance_for(nobody, b.pk)
    with pytest.raises(PermissionDenied):
        require(nobody, "system.settings")
    require(users["owner"], "system.settings")


def test_validate_codes():
    assert validate_codes(["instance.view", "instance.view", "system.audit"]) == ["instance.view", "system.audit"]
    with pytest.raises(ValueError, match="Unknown permission codes"):
        validate_codes(["instance.view", "root.everything"])


def test_code_sets_partition():
    assert SYSTEM_CODES | INSTANCE_CODES == ALL_CODES
    assert not SYSTEM_CODES & INSTANCE_CODES
    assert "instance.create" in SYSTEM_CODES  # creating servers is a global capability
