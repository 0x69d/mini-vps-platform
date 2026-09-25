import pytest
from conftest import linux_kvm_profile, macos_profile

from mini_vps.errors import PlatformUnsupported
from mini_vps.planning import (
    FILTER_ATTACH_APPLY_MODE,
    Action,
    ApplyMode,
    apply_mode,
    check_platform,
    diff_keys,
    field_apply_mode,
    filter_attachment_changes,
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


# --- filters / egress の条件付き反映方式 ---

_SSH = [{"port": 22, "protocol": "tcp"}]
_WEB = [{"port": 80, "protocol": "tcp"}]
_INTERNET = [{"cidr": "0.0.0.0/0"}]
_LAN_BLOCKED = [{"action": "drop", "cidr": "10.0.0.0/8"}, {"cidr": "0.0.0.0/0"}]


@pytest.mark.parametrize(
    ("old", "new", "changes"),
    [
        ({}, {"egress": []}, True),
        ({"egress": []}, {}, True),
        ({"filters": _SSH}, {"filters": None}, True),
        # filters を外しても egress が残れば filter は要るまま
        ({"filters": _SSH, "egress": []}, {"egress": []}, False),
        ({"filters": _SSH}, {"filters": _SSH, "egress": _INTERNET}, False),
        ({"egress": _INTERNET}, {"egress": _LAN_BLOCKED}, False),
        ({}, {}, False),
    ],
)
def test_filter_attachment_changes(old, new, changes):
    assert filter_attachment_changes(_spec(**old), _spec(**new)) is changes


def test_field_apply_mode_is_live_when_only_rules_change():
    old, new = _spec(filters=_SSH), _spec(filters=_WEB)
    assert field_apply_mode("filters", old, new) is ApplyMode.LIVE


def test_field_apply_mode_uses_attach_mode_when_filter_presence_changes(
    monkeypatch,
):
    old, new = _spec(), _spec(egress=_INTERNET)
    assert field_apply_mode("egress", old, new) is FILTER_ATTACH_APPLY_MODE
    monkeypatch.setattr("mini_vps.planning.FILTER_ATTACH_APPLY_MODE", ApplyMode.OFFLINE)
    assert field_apply_mode("egress", old, new) is ApplyMode.OFFLINE
    # filters / egress 以外は静的な表のまま
    assert field_apply_mode("memory", old, new) is ApplyMode.OFFLINE


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"filters": _SSH}, {"filters": _WEB}),
        ({"egress": _INTERNET}, {"egress": _LAN_BLOCKED}),
        ({"filters": _SSH}, {"filters": _SSH, "egress": _INTERNET}),
        ({"filters": _SSH, "egress": []}, {"egress": []}),
    ],
)
def test_plan_change_converges_rule_changes_while_running(old, new):
    change = plan_change(_spec(**old), _spec(**new), running=True)
    assert change.action is Action.CONVERGE
    assert change.offline_keys == frozenset()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({}, {"egress": _INTERNET}),
        ({"egress": []}, {}),
        ({"filters": _SSH}, {}),
    ],
)
def test_plan_change_filter_attach_follows_attach_mode(old, new, monkeypatch):
    # 既定(LIVE)では稼働中でも収束する
    change = plan_change(_spec(**old), _spec(**new), running=True)
    assert change.action is Action.CONVERGE

    # OFFLINE に切り替えると稼働中は拒否、停止中は収束
    monkeypatch.setattr("mini_vps.planning.FILTER_ATTACH_APPLY_MODE", ApplyMode.OFFLINE)
    change = plan_change(_spec(**old), _spec(**new), running=True)
    assert change.action is Action.BLOCKED_RUNNING
    assert change.offline_keys
    change = plan_change(_spec(**old), _spec(**new), running=False)
    assert change.action is Action.CONVERGE


def test_plan_change_blocks_memory_even_with_live_filter_change():
    change = plan_change(
        _spec(filters=_SSH), _spec(filters=_WEB, memory=2048), running=True
    )
    assert change.action is Action.BLOCKED_RUNNING
    assert change.offline_keys == {"memory"}


def test_plan_change_noop_for_existing_vm_without_egress_in_metadata():
    # egress 追加前に作った VM の metadata(キー無し)を ServerSpec で補完したもの
    old = ServerSpec(
        **{k: v for k, v in _spec(filters=_SSH).items() if k != "egress"}
    ).model_dump()
    assert plan_change(old, _spec(filters=_SSH), running=True).action is Action.NOOP


@pytest.mark.parametrize("egress", [[], _INTERNET])
def test_check_platform_rejects_egress_on_macos(egress):
    with pytest.raises(PlatformUnsupported, match="egress"):
        check_platform(_spec(egress=egress), macos_profile())


def test_check_platform_accepts_egress_on_linux():
    check_platform(_spec(egress=_LAN_BLOCKED), linux_kvm_profile())
