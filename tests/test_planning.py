import pytest
from conftest import linux_kvm_profile, macos_profile

from mini_vps.errors import PlatformUnsupported
from mini_vps.planning import (
    Action,
    ApplyMode,
    apply_mode,
    check_platform,
    diff_keys,
    plan_change,
)
from mini_vps.spec import ServerSpec


def _spec(**overrides):
    base = {
        "name": "web-1",
        "memory": 1024,
        "vcpus": 2,
        "base_image": "ubuntu-24.04.img",
        "disk": 10,
    }
    base.update(overrides)
    return ServerSpec(**base).model_dump()


def test_apply_mode_defaults_to_recreate_for_unknown_fields():
    assert apply_mode("networks") is ApplyMode.RECREATE
    assert apply_mode("autostart") is ApplyMode.LIVE
    assert apply_mode("memory") is ApplyMode.OFFLINE


def test_diff_keys_lists_changed_fields():
    assert diff_keys(_spec(), _spec(memory=2048, vcpus=4)) == {"memory", "vcpus"}


def test_plan_change_create_when_absent():
    assert plan_change(None, _spec(), running=False).action is Action.CREATE


def test_plan_change_noop_when_equal():
    assert plan_change(_spec(), _spec(), running=True).action is Action.NOOP


def test_plan_change_conflict_on_recreate_field():
    change = plan_change(_spec(), _spec(disk=20, memory=2048), running=False)
    assert change.action is Action.CONFLICT
    assert change.recreate_keys == {"disk"}


def test_plan_change_blocks_offline_field_while_running():
    change = plan_change(_spec(), _spec(memory=2048), running=True)
    assert change.action is Action.BLOCKED_RUNNING
    assert change.offline_keys == {"memory"}


def test_plan_change_converges_offline_field_when_stopped():
    change = plan_change(_spec(), _spec(memory=2048), running=False)
    assert change.action is Action.CONVERGE


def test_plan_change_converges_live_field_while_running():
    change = plan_change(_spec(), _spec(autostart=False), running=True)
    assert change.action is Action.CONVERGE
    assert change.diff_keys == {"autostart"}


def test_check_platform_accepts_everything_on_linux():
    spec = _spec(
        networks=["default", {"name": "seg1", "address": "192.168.201.10/24"}],
        filters=[{"port": 22, "protocol": "tcp"}],
    )
    check_platform(spec, linux_kvm_profile())


def test_check_platform_accepts_default_network_on_macos():
    check_platform(_spec(), macos_profile())


@pytest.mark.parametrize(
    "overrides",
    [
        {"networks": ["seg1"]},
        {"networks": ["default", "seg1"]},
        {"networks": [{"name": "default", "address": "10.0.2.20/24"}]},
        {"filters": [{"port": 22, "protocol": "tcp"}]},
        {"filters": []},
    ],
)
def test_check_platform_rejects_unsupported_features_on_macos(overrides):
    with pytest.raises(PlatformUnsupported):
        check_platform(_spec(**overrides), macos_profile())
