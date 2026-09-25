import threading
from unittest.mock import MagicMock

import libvirt
import pytest
from conftest import linux_kvm_profile, macos_profile, make_libvirt_error

from mini_vps import doctor
from mini_vps.doctor import (
    ERROR,
    OK,
    WARN,
    Resource,
    accel_check,
    autostart_checks,
    base_image_checks,
    classify_nwfilter,
    classify_volume,
    has_error,
    lock_dir_check,
    network_checks,
    orphan_checks,
    split_orphans,
)
from mini_vps.manager import ServerManager
from mini_vps.platform_profile import set_profile

# --- classify_volume / classify_nwfilter ---


@pytest.mark.parametrize(
    ("pool", "name", "expected"),
    [
        (
            "vps-pool",
            "web-1.qcow2",
            Resource("overlay", "web-1", "web-1.qcow2", "vps-pool"),
        ),
        (
            "vps-pool",
            "web-1.snap-before-upgrade.qcow2",
            Resource(
                "snapshot", "web-1", "web-1.snap-before-upgrade.qcow2", "vps-pool"
            ),
        ),
        (
            "vps-seeds",
            "a-seed-seed.iso",
            Resource("seed", "a-seed", "a-seed-seed.iso", "vps-seeds"),
        ),
        ("vps-pool", "notes.txt", None),
        ("vps-pool", "web-1-seed.iso", None),
        ("vps-seeds", "web-1.qcow2", None),
        ("images", "ubuntu.qcow2", None),
    ],
)
def test_classify_volume(pool, name, expected):
    assert classify_volume(pool, name) == expected


def test_classify_nwfilter_only_matches_minivps_prefix():
    assert classify_nwfilter("minivps-web-1") == Resource(
        "nwfilter", "web-1", "minivps-web-1"
    )
    assert classify_nwfilter("clean-traffic") is None


def test_resource_label():
    assert (
        Resource("seed", "a", "a-seed.iso", "vps-seeds").label == "vps-seeds/a-seed.iso"
    )
    assert Resource("nwfilter", "a", "minivps-a").label == "nwfilter/minivps-a"


# --- split_orphans ---


def test_split_orphans_separates_missing_and_unmanaged_domains():
    orphan = Resource("overlay", "gone", "gone.qcow2", "vps-pool")
    owned = Resource("overlay", "web-1", "web-1.qcow2", "vps-pool")
    shadowed = Resource("seed", "other", "other-seed.iso", "vps-seeds")

    orphans, shadow = split_orphans(
        [orphan, owned, shadowed], {"web-1", "other"}, {"web-1"}
    )

    assert orphans == [orphan]
    assert shadow == [shadowed]


# --- 純粋な検査関数 ---


def test_network_checks_levels():
    results = network_checks(
        {"default": ["web-1"], "seg1": ["db-1", "app-1"], "gone": ["x"]},
        {"default": (True, True), "seg1": (False, False), "gone": None},
    )

    by_check = {r["check"]: r for r in results}
    assert by_check["network:default"]["level"] == OK
    assert by_check["network:seg1"]["level"] == WARN
    assert "非アクティブ" in by_check["network:seg1"]["detail"]
    assert "autostart" in by_check["network:seg1"]["detail"]
    assert by_check["network:gone"]["level"] == ERROR
    assert "x" in by_check["network:gone"]["detail"]


def test_base_image_checks_errors_on_missing_image():
    results = base_image_checks(
        {"ubuntu.img": ["web-1"], "gone.img": ["db-1"]}, {"ubuntu.img"}
    )

    assert [(r["check"], r["level"]) for r in results] == [
        ("base_image:gone.img", ERROR),
        ("base_image:ubuntu.img", OK),
    ]


def test_base_image_checks_errors_when_pool_missing():
    results = base_image_checks({"ubuntu.img": ["web-1"]}, None)

    assert results == [
        {"level": ERROR, "check": "pool:images", "detail": "base image 用プールが無い"}
    ]


def test_lock_dir_check_ok_when_writable(tmp_path):
    assert lock_dir_check(str(tmp_path))["level"] == OK


def test_lock_dir_check_ok_when_creatable(tmp_path):
    result = lock_dir_check(str(tmp_path / "a" / "b"))

    assert result["level"] == OK
    assert "未作成" in result["detail"]


def test_lock_dir_check_warns_when_not_writable(tmp_path):
    result = lock_dir_check(str(tmp_path), access=lambda path, mode: False)

    assert result["level"] == WARN


def test_accel_check_warns_on_tcg():
    assert accel_check("tcg")["level"] == WARN
    assert accel_check("kvm")["level"] == OK


def test_autostart_checks_reports_mismatch_only():
    results = autostart_checks(
        {"web-1": True, "web-2": False, "gone": True},
        {"web-1": False, "web-2": False},
    )

    assert results == [
        {
            "level": WARN,
            "check": "autostart:web-1",
            "detail": "spec は True だが domain は False",
        }
    ]


def test_autostart_checks_ok_when_consistent():
    assert autostart_checks({"a": True}, {"a": True})[0]["level"] == OK


def test_orphan_checks():
    orphan = Resource("overlay", "gone", "gone.qcow2", "vps-pool")
    shadowed = Resource("nwfilter", "other", "minivps-other")

    results = orphan_checks([orphan], [shadowed])

    assert [r["check"] for r in results] == [
        "orphan:vps-pool/gone.qcow2",
        "unmanaged:nwfilter/minivps-other",
    ]
    assert orphan_checks([], [])[0]["level"] == OK


def test_has_error():
    assert has_error([{"level": WARN}, {"level": ERROR}])
    assert not has_error([{"level": OK}, {"level": WARN}])


# --- libvirt を伴う検査(Mock) ---


def _named(name, **attrs):
    obj = MagicMock()
    obj.name.return_value = name
    for key, value in attrs.items():
        getattr(obj, key).return_value = value
    return obj


class FakeHost:
    """doctor が触る libvirt 接続の最小限の偽物。"""

    def __init__(self, domains, volumes, nwfilters=(), networks=None, images=None):
        self.conn = MagicMock()
        self.domains = {d: _named(d, autostart=1) for d in domains}
        self.volumes = {pool: list(names) for pool, names in volumes.items()}
        self.nwfilters = list(nwfilters)
        self.networks = {"default": (True, True)} if networks is None else networks
        self.images = ["ubuntu.img"] if images is None else images
        self.pools = {}
        for pool_name in ["vps-pool", "vps-seeds", "images"]:
            pool = _named(pool_name)
            pool.listAllVolumes.side_effect = lambda p=pool_name: self._vols(p)
            pool.storageVolLookupByName.side_effect = lambda name, p=pool_name: (
                self._vol(p, name)
            )
            self.pools[pool_name] = pool
        self.conn.listAllStoragePools.side_effect = lambda: list(self.pools.values())
        self.conn.storagePoolLookupByName.side_effect = lambda n: self.pools[n]
        self.conn.listAllDomains.side_effect = lambda: list(self.domains.values())
        self.conn.lookupByName.side_effect = self._lookup
        self.conn.listAllNWFilters.side_effect = lambda: [
            _named(n) for n in self.nwfilters
        ]
        self.conn.nwfilterLookupByName.side_effect = self._nwfilter
        self.conn.networkLookupByName.side_effect = self._network
        self.deleted = []

    def _vols(self, pool):
        names = self.images if pool == "images" else self.volumes.get(pool, [])
        return [_named(n) for n in names]

    def _vol(self, pool, name):
        if name not in self.volumes.get(pool, []):
            raise make_libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL)
        vol = _named(name)
        vol.delete.side_effect = lambda flags: (
            self.volumes[pool].remove(name),
            self.deleted.append(f"{pool}/{name}"),
        )
        return vol

    def _nwfilter(self, name):
        if name not in self.nwfilters:
            raise make_libvirt_error(libvirt.VIR_ERR_NO_NWFILTER)
        f = _named(name)
        f.undefine.side_effect = lambda: (
            self.nwfilters.remove(name),
            self.deleted.append(f"nwfilter/{name}"),
        )
        return f

    def _lookup(self, name):
        if name not in self.domains:
            raise make_libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
        return self.domains[name]

    def _network(self, name):
        if name not in self.networks:
            raise make_libvirt_error(libvirt.VIR_ERR_NO_NETWORK)
        active, autostart = self.networks[name]
        return _named(name, isActive=int(active), autostart=int(autostart))


def _manager(host, specs):
    mgr = ServerManager(host.conn)
    mgr.managed_specs = lambda: {n: s for n, s in specs.items() if n in host.domains}
    return mgr


SPEC = {"base_image": "ubuntu.img", "networks": ["default"], "autostart": True}


def test_find_orphans_ignores_nwfilters_without_support():
    set_profile(macos_profile())
    host = FakeHost([], {"vps-pool": ["gone.qcow2"]}, nwfilters=["minivps-gone"])

    orphans, _ = doctor.find_orphans(_manager(host, {}))

    assert [o.label for o in orphans] == ["vps-pool/gone.qcow2"]
    host.conn.listAllNWFilters.assert_not_called()


def test_run_checks_healthy_host_has_no_error():
    host = FakeHost(
        ["web-1"],
        {"vps-pool": ["web-1.qcow2"], "vps-seeds": ["web-1-seed.iso"]},
        nwfilters=["minivps-web-1"],
    )

    results = doctor.run_checks(_manager(host, {"web-1": SPEC}))

    assert not has_error(results)
    assert {r["check"] for r in results} >= {
        "network:default",
        "base_image:ubuntu.img",
        "lock_dir",
        "accelerator",
        "autostart",
        "orphans",
    }


def test_run_checks_reports_problems():
    host = FakeHost(
        ["web-1"],
        {"vps-pool": ["web-1.qcow2", "gone.qcow2"]},
        networks={},
        images=[],
    )
    host.domains["web-1"].autostart.return_value = 0

    results = doctor.run_checks(_manager(host, {"web-1": SPEC}))

    levels = {r["check"]: r["level"] for r in results}
    assert levels["network:default"] == ERROR
    assert levels["base_image:ubuntu.img"] == ERROR
    assert levels["autostart:web-1"] == WARN
    assert levels["orphan:vps-pool/gone.qcow2"] == WARN


def test_run_checks_skips_networks_in_user_mode():
    set_profile(macos_profile())
    host = FakeHost(["web-1"], {}, networks={})

    results = doctor.run_checks(_manager(host, {"web-1": SPEC}))

    assert not any(r["check"].startswith("network:") for r in results)
    host.conn.networkLookupByName.assert_not_called()


def test_run_checks_warns_on_tcg():
    set_profile(linux_kvm_profile(accel="tcg"))
    host = FakeHost([], {})

    results = doctor.run_checks(_manager(host, {}))

    assert {"level": WARN, "check": "accelerator"}.items() <= next(
        r for r in results if r["check"] == "accelerator"
    ).items()


# --- gc ---


def test_gc_dry_run_deletes_nothing():
    host = FakeHost(
        ["web-1"],
        {
            "vps-pool": ["web-1.qcow2", "gone.qcow2", "gone.snap-1.qcow2"],
            "vps-seeds": ["gone-seed.iso"],
        },
        nwfilters=["minivps-gone", "minivps-web-1"],
    )

    result = doctor.gc(_manager(host, {"web-1": SPEC}))

    assert result["applied"] is False
    assert sorted(f"{o['pool']}/{o['name']}" for o in result["orphans"]) == [
        "None/minivps-gone",
        "vps-pool/gone.qcow2",
        "vps-pool/gone.snap-1.qcow2",
        "vps-seeds/gone-seed.iso",
    ]
    assert result["removed"] == []
    assert host.deleted == []


def test_gc_apply_removes_orphans_only():
    host = FakeHost(
        ["web-1"],
        {
            "vps-pool": ["web-1.qcow2", "gone.qcow2", "gone.snap-1.qcow2"],
            "vps-seeds": ["web-1-seed.iso", "gone-seed.iso"],
        },
        nwfilters=["minivps-gone", "minivps-web-1"],
    )

    result = doctor.gc(_manager(host, {"web-1": SPEC}), apply=True)

    assert sorted(host.deleted) == [
        "nwfilter/minivps-gone",
        "vps-pool/gone.qcow2",
        "vps-pool/gone.snap-1.qcow2",
        "vps-seeds/gone-seed.iso",
    ]
    assert len(result["removed"]) == 4
    assert host.volumes["vps-pool"] == ["web-1.qcow2"]


def test_gc_apply_keeps_resources_of_unmanaged_domain():
    host = FakeHost(["other"], {"vps-pool": ["other.qcow2"]})

    result = doctor.gc(_manager(host, {}), apply=True)

    assert result["orphans"] == []
    assert host.deleted == []


def test_gc_apply_rechecks_under_lock_and_skips_created_domain():
    """検出後・ロック取得前に create が domain を define したら消さない。"""
    host = FakeHost([], {"vps-pool": ["new.qcow2"], "vps-seeds": ["new-seed.iso"]})
    mgr = _manager(host, {})
    real_locked = mgr._locked

    def _locked(name):
        # ロックを待っている間に、進行中の create が define を終えた状況を再現する。
        host.domains[name] = _named(name, autostart=1)
        return real_locked(name)

    mgr._locked = _locked

    result = doctor.gc(mgr, apply=True)

    assert host.deleted == []
    assert result["removed"] == []
    assert {s["reason"] for s in result["skipped"]} == {"domain が作成された"}


def test_gc_apply_waits_for_in_progress_create():
    """create が name ロックを持つ間は gc が待ち、完了後の再判定で消さない。"""
    host = FakeHost([], {"vps-pool": ["web-9.qcow2"]})
    mgr = _manager(host, {})
    locked = threading.Event()
    release = threading.Event()

    def _create():
        with mgr._locked("web-9"):
            locked.set()
            release.wait(5)
            host.domains["web-9"] = _named("web-9", autostart=1)

    creator = threading.Thread(target=_create)
    creator.start()
    locked.wait(5)
    result = {}
    gc_thread = threading.Thread(target=lambda: result.update(doctor.gc(mgr, True)))
    gc_thread.start()
    gc_thread.join(0.2)
    assert gc_thread.is_alive()  # ロック待ち
    release.set()
    creator.join(5)
    gc_thread.join(5)

    assert host.deleted == []
    assert result["skipped"][0]["reason"] == "domain が作成された"


def test_gc_apply_records_already_gone_and_failures(caplog):
    host = FakeHost([], {"vps-pool": ["a.qcow2", "b.qcow2"]})
    mgr = _manager(host, {})
    real_vol = host._vol

    def _vol(pool, name):
        if name == "a.qcow2":
            raise make_libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL)
        vol = real_vol(pool, name)
        vol.delete.side_effect = make_libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)
        return vol

    host.pools["vps-pool"].storageVolLookupByName.side_effect = lambda n: _vol(
        "vps-pool", n
    )

    with caplog.at_level("WARNING", logger="mini_vps.doctor"):
        result = doctor.gc(mgr, apply=True)

    reasons = {s["name"]: s["reason"] for s in result["skipped"]}
    assert reasons["a.qcow2"] == "既に無い"
    assert reasons["b.qcow2"] == "mock error"
    assert result["removed"] == []
    assert any("b.qcow2" in r.getMessage() for r in caplog.records)


# --- ServerManager への委譲 ---


def test_manager_doctor_and_gc_delegate(monkeypatch):
    mgr = ServerManager(MagicMock())
    run_checks = MagicMock(return_value=[])
    gc = MagicMock(return_value={})
    monkeypatch.setattr("mini_vps.doctor.run_checks", run_checks)
    monkeypatch.setattr("mini_vps.doctor.gc", gc)

    mgr.doctor()
    mgr.gc(apply=True)

    run_checks.assert_called_once_with(mgr)
    gc.assert_called_once_with(mgr, apply=True)


def test_managed_specs_skips_domains_vanished_during_listing(monkeypatch):
    conn = MagicMock()
    alive = _named("web-1")
    vanished = MagicMock()
    vanished.name.side_effect = make_libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
    unmanaged = _named("other")
    conn.listAllDomains.return_value = [alive, vanished, unmanaged]
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda d: d is not unmanaged)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: {"name": "web-1"})

    assert ServerManager(conn).managed_specs() == {"web-1": {"name": "web-1"}}


def test_managed_specs_reraises_other_errors(monkeypatch):
    conn = MagicMock()
    conn.listAllDomains.return_value = [MagicMock()]
    monkeypatch.setattr(
        "mini_vps.manager._is_managed",
        MagicMock(side_effect=make_libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)),
    )

    with pytest.raises(libvirt.libvirtError):
        ServerManager(conn).managed_specs()
