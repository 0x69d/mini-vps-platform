from unittest.mock import MagicMock

import libvirt
import pytest
from conftest import make_libvirt_error

from mini_vps import admission
from mini_vps.admission import (
    GIB,
    AdmissionSettings,
    Allocation,
    HostCapacity,
    allocations_from_specs,
    evaluate,
    load_settings,
    memory_limit_mib,
    memory_reserve_mib,
    needs_recheck,
)
from mini_vps.errors import InsufficientCapacity
from mini_vps.manager import ServerManager
from mini_vps.spec import ServerSpec

HOST = HostCapacity(memory_mib=32768, cpus=8)
DEFAULT = AdmissionSettings()


def _spec(**overrides):
    spec = {
        "name": "web-1",
        "memory": 1024,
        "vcpus": 2,
        "base_image": "ubuntu-24.04.img",
        "disk": 10,
    }
    spec.update(overrides)
    return spec


# --- load_settings ---


def test_load_settings_defaults_when_env_empty():
    assert load_settings({}) == AdmissionSettings(None, 1.0)


def test_load_settings_reads_env():
    settings = load_settings(
        {"MINIVPS_MEMORY_RESERVE_MIB": "4096", "MINIVPS_MEMORY_OVERCOMMIT": "1.5"}
    )

    assert settings == AdmissionSettings(4096, 1.5)


@pytest.mark.parametrize(
    "env",
    [
        {"MINIVPS_MEMORY_RESERVE_MIB": "-1"},
        {"MINIVPS_MEMORY_RESERVE_MIB": "abc"},
        {"MINIVPS_MEMORY_OVERCOMMIT": "0"},
        {"MINIVPS_MEMORY_OVERCOMMIT": "nan"},
    ],
)
def test_load_settings_rejects_invalid_values(env):
    with pytest.raises(ValueError):
        load_settings(env)


# --- memory_reserve_mib / memory_limit_mib ---


def test_reserve_is_ten_percent_on_large_host():
    assert memory_reserve_mib(65536, DEFAULT) == 6554


def test_reserve_is_at_least_two_gib_on_small_host():
    assert memory_reserve_mib(8192, DEFAULT) == 2048


def test_reserve_uses_explicit_setting():
    assert memory_reserve_mib(65536, AdmissionSettings(memory_reserve_mib=1024)) == 1024


def test_limit_applies_overcommit_after_reserve():
    settings = AdmissionSettings(memory_reserve_mib=2048, memory_overcommit=1.5)

    assert memory_limit_mib(10240, settings) == (10240 - 2048) * 1.5


def test_limit_is_zero_when_reserve_exceeds_host():
    assert memory_limit_mib(1024, DEFAULT) == 0


# --- evaluate ---


def test_evaluate_accepts_when_everything_fits():
    decision = evaluate(
        HOST,
        [Allocation("db-1", 4096, 4, 20)],
        _spec(),
        DEFAULT,
        base_image_bytes=3 * GIB,
        pool_available_bytes=100 * GIB,
    )

    assert decision.ok
    assert decision.warnings == ()


def test_evaluate_counts_every_managed_vm_regardless_of_state():
    # ホスト 32768 - 予約 3277 = 上限 29491。既存 28000 + 新規 2048 は超える。
    decision = evaluate(
        HOST,
        [Allocation("db-1", 20000, 1, 10), Allocation("db-2", 8000, 1, 10)],
        _spec(memory=2048),
        DEFAULT,
    )

    assert not decision.ok
    assert "メモリが足りない" in decision.reasons[0]
    assert "上限 29491 MiB" in decision.reasons[0]


def test_evaluate_accepts_exactly_at_the_limit():
    limit = memory_limit_mib(HOST.memory_mib, DEFAULT)

    decision = evaluate(HOST, [], _spec(memory=limit), DEFAULT)

    assert decision.ok


def test_evaluate_overcommit_allows_more_memory():
    spec = _spec(memory=40000)

    assert not evaluate(HOST, [], spec, DEFAULT).ok
    assert evaluate(HOST, [], spec, AdmissionSettings(memory_overcommit=1.5)).ok


def test_evaluate_excludes_own_current_allocation():
    """収束で拡張するとき、自分自身の現在の割当を二重計上しない。"""
    limit = memory_limit_mib(HOST.memory_mib, DEFAULT)
    allocations = [Allocation("web-1", limit, 2, 10)]

    decision = evaluate(HOST, allocations, _spec(memory=limit), DEFAULT)

    assert decision.ok


def test_evaluate_rejects_vcpus_above_host_cpus():
    decision = evaluate(HOST, [], _spec(vcpus=9), DEFAULT)

    assert not decision.ok
    assert "vCPU" in decision.reasons[0]


def test_evaluate_allows_vcpu_overcommit_in_total():
    allocations = [Allocation(f"vm-{i}", 128, 8, 10) for i in range(4)]

    assert evaluate(HOST, allocations, _spec(vcpus=8), DEFAULT).ok


def test_evaluate_rejects_disk_smaller_than_base_image():
    decision = evaluate(HOST, [], _spec(disk=3), DEFAULT, base_image_bytes=3 * GIB + 1)

    assert not decision.ok
    assert "disk が小さすぎる" in decision.reasons[0]


def test_evaluate_accepts_disk_equal_to_base_image():
    assert evaluate(HOST, [], _spec(disk=3), DEFAULT, base_image_bytes=3 * GIB).ok


def test_evaluate_only_warns_when_pool_is_short():
    decision = evaluate(
        HOST,
        [Allocation("db-1", 1024, 1, 50)],
        _spec(disk=20),
        DEFAULT,
        pool_available_bytes=60 * GIB,
    )

    assert decision.ok
    assert "disk の合計 70 GiB" in decision.warnings[0]


def test_evaluate_collects_multiple_reasons():
    decision = evaluate(
        HOST,
        [],
        _spec(memory=10**6, vcpus=64, disk=1),
        DEFAULT,
        base_image_bytes=GIB * 2,
    )

    assert len(decision.reasons) == 3


def test_evaluate_skips_disk_checks_when_values_absent():
    decision = evaluate(HOST, [], _spec(disk=1), DEFAULT)

    assert decision.ok
    assert decision.warnings == ()


# --- needs_recheck / allocations_from_specs ---


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ({"memory": 1024, "vcpus": 2}, {"memory": 2048, "vcpus": 2}, True),
        ({"memory": 1024, "vcpus": 2}, {"memory": 1024, "vcpus": 4}, True),
        ({"memory": 2048, "vcpus": 4}, {"memory": 1024, "vcpus": 2}, False),
        ({"memory": 1024, "vcpus": 2}, {"memory": 1024, "vcpus": 2}, False),
    ],
)
def test_needs_recheck_only_when_growing(old, new, expected):
    assert needs_recheck(old, new) is expected


def test_allocations_from_specs():
    specs = {"web-1": _spec(), "db-1": _spec(name="db-1", memory=4096, disk=40)}

    assert allocations_from_specs(specs) == [
        Allocation("web-1", 1024, 2, 10),
        Allocation("db-1", 4096, 2, 40),
    ]


# --- libvirt からの収集 ---


def _conn(memory_mib=32768, cpus=8, base_bytes=3 * GIB, available=100 * GIB):
    conn = MagicMock()
    conn.getInfo.return_value = ["x86_64", memory_mib, cpus, 2100, 1, 1, cpus, 1]
    base_pool = MagicMock()
    base_pool.storageVolLookupByName.return_value.info.return_value = [
        0,
        base_bytes,
        GIB // 2,
    ]
    vps_pool = MagicMock()
    vps_pool.info.return_value = [2, 200 * GIB, 100 * GIB - available, available]
    pools = {"images": base_pool, "vps-pool": vps_pool}
    conn.storagePoolLookupByName.side_effect = lambda name: pools[name]
    return conn


def test_host_capacity_reads_memory_mib_and_cpus():
    assert admission.host_capacity(_conn(memory_mib=8418, cpus=4)) == HostCapacity(
        8418, 4
    )


def test_base_image_bytes_refreshes_and_reads_capacity():
    conn = _conn(base_bytes=5 * GIB)

    assert admission.base_image_bytes(conn, "ubuntu-24.04.img") == 5 * GIB
    conn.storagePoolLookupByName("images").refresh.assert_called_once_with(0)


def test_base_image_bytes_is_none_when_volume_missing():
    conn = _conn()
    conn.storagePoolLookupByName(
        "images"
    ).storageVolLookupByName.side_effect = make_libvirt_error(
        libvirt.VIR_ERR_NO_STORAGE_VOL
    )

    assert admission.base_image_bytes(conn, "missing.img") is None


def test_base_image_bytes_reraises_other_errors():
    conn = MagicMock()
    conn.storagePoolLookupByName.side_effect = make_libvirt_error(
        libvirt.VIR_ERR_INTERNAL_ERROR
    )

    with pytest.raises(libvirt.libvirtError):
        admission.base_image_bytes(conn, "x.img")


def test_pool_available_bytes_is_none_when_pool_missing():
    conn = MagicMock()
    conn.storagePoolLookupByName.side_effect = make_libvirt_error(
        libvirt.VIR_ERR_NO_STORAGE_POOL
    )

    assert admission.pool_available_bytes(conn) is None


# --- check_capacity ---


def test_check_capacity_raises_with_reasons():
    conn = _conn(memory_mib=8192)

    with pytest.raises(InsufficientCapacity, match="web-1: メモリが足りない"):
        admission.check_capacity(conn, _spec(memory=8192), lambda: {}, env={})


def test_check_capacity_logs_pool_warning_and_accepts(caplog):
    conn = _conn(available=5 * GIB)

    with caplog.at_level("WARNING", logger="mini_vps.admission"):
        decision = admission.check_capacity(conn, _spec(), lambda: {}, env={})

    assert decision.ok
    assert any("overlay 用プールの空き" in r.getMessage() for r in caplog.records)


def test_check_capacity_without_disk_skips_storage_lookups():
    conn = _conn()

    admission.check_capacity(conn, _spec(), lambda: {}, include_disk=False, env={})

    conn.storagePoolLookupByName.assert_not_called()


def test_check_capacity_does_not_log_spec_body(caplog):
    conn = _conn(memory_mib=4096)
    spec = _spec(memory=4096, startup_script="opencode-sakura-ai-engine")

    with caplog.at_level("DEBUG", logger="mini_vps"):
        with pytest.raises(InsufficientCapacity):
            admission.check_capacity(conn, spec, lambda: {}, env={})

    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert "opencode-sakura-ai-engine" not in emitted
    assert "ubuntu-24.04.img" not in emitted


# --- ServerManager.create との結合 ---


def test_create_checks_capacity_before_provision(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    calls = []
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: None)
    monkeypatch.setattr(
        "mini_vps.manager.check_capacity",
        lambda c, spec, specs, **kw: calls.append(("check", kw)),
    )
    monkeypatch.setattr(
        "mini_vps.manager.provision",
        lambda c, spec, secrets=None: calls.append(("provision", {})) or MagicMock(),
    )
    monkeypatch.setattr("mini_vps.manager._write_spec", MagicMock())
    mgr.get = MagicMock(return_value={})

    mgr.create(_spec())

    assert calls == [("check", {}), ("provision", {})]


def test_create_rejects_without_provision_or_teardown(monkeypatch):
    """容量不足は何も作る前に拒否し、巻き戻し(teardown)も走らせない。"""
    conn = _conn(memory_mib=4096)
    mgr = ServerManager(conn)
    monkeypatch.setattr("mini_vps.manager.check_capacity", admission.check_capacity)
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: None)
    monkeypatch.setattr(mgr, "managed_specs", lambda: {})
    provision_mock = MagicMock()
    teardown_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.provision", provision_mock)
    monkeypatch.setattr("mini_vps.manager.teardown", teardown_mock)

    with pytest.raises(InsufficientCapacity):
        mgr.create(_spec(memory=4096))

    provision_mock.assert_not_called()
    teardown_mock.assert_not_called()


def _converge_setup(monkeypatch, old_spec):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda d: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: dict(old_spec))
    monkeypatch.setattr("mini_vps.manager._write_spec", MagicMock())
    mgr._converge = MagicMock(return_value=dom)
    mgr.get = MagicMock(return_value={})
    check = MagicMock()
    monkeypatch.setattr("mini_vps.manager.check_capacity", check)
    return mgr, check


def test_create_checks_capacity_when_converge_grows_memory(monkeypatch):
    old_spec = ServerSpec(**_spec()).model_dump()
    mgr, check = _converge_setup(monkeypatch, old_spec)
    new_spec = dict(old_spec, memory=4096)

    mgr.create(new_spec)

    check.assert_called_once()
    assert check.call_args.kwargs == {"include_disk": False}
    assert check.call_args.args[1] == new_spec


@pytest.mark.parametrize(
    "overrides", [{"memory": 512}, {"vcpus": 1}, {"autostart": False}]
)
def test_create_skips_capacity_check_when_not_growing(monkeypatch, overrides):
    old_spec = ServerSpec(**_spec()).model_dump()
    mgr, check = _converge_setup(monkeypatch, old_spec)

    mgr.create(dict(old_spec, **overrides))

    check.assert_not_called()
    mgr._converge.assert_called_once()


def test_create_converge_rejects_before_converging(monkeypatch):
    old_spec = ServerSpec(**_spec()).model_dump()
    mgr, check = _converge_setup(monkeypatch, old_spec)
    check.side_effect = InsufficientCapacity("web-1: メモリが足りない")

    with pytest.raises(InsufficientCapacity):
        mgr.create(dict(old_spec, memory=65536))

    mgr._converge.assert_not_called()


def test_create_converge_does_not_double_count_self(monkeypatch):
    """実物の check_capacity を通し、自分の現在の割当が除かれることを確かめる。"""
    old_spec = ServerSpec(**_spec(memory=16000)).model_dump()
    mgr, _check = _converge_setup(monkeypatch, old_spec)
    mgr.conn = _conn(memory_mib=32768)
    monkeypatch.setattr("mini_vps.manager.check_capacity", admission.check_capacity)
    monkeypatch.setattr(mgr, "managed_specs", lambda: {"web-1": old_spec})

    # 上限 29491 MiB。自分を二重計上すると 16000 + 20000 で超えてしまう。
    mgr.create(dict(old_spec, memory=20000))

    mgr._converge.assert_called_once()
