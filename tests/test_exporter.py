from unittest.mock import MagicMock

import libvirt

from mini_vps.exporter import (
    DomainCollector,
    _parse_domain_stats,
    _parse_host_stats,
    main,
)

RAW_RUNNING = {
    "state.state": libvirt.VIR_DOMAIN_RUNNING,
    "state.reason": 1,
    "cpu.time": 12_300_000_000,
    "balloon.current": 524288,
    "balloon.maximum": 1048576,
    "balloon.available": 1000000,
    "balloon.usable": 600000,
    "vcpu.current": 2,
    "net.count": 1,
    "net.0.name": "vnet0",
    "net.0.rx.bytes": 100,
    "net.0.rx.pkts": 10,
    "net.0.tx.bytes": 200,
    "net.0.tx.pkts": 20,
    "block.count": 1,
    "block.0.name": "vda",
    "block.0.rd.bytes": 300,
    "block.0.rd.reqs": 30,
    "block.0.wr.bytes": 400,
    "block.0.wr.reqs": 40,
}


def _samples_by_name(families, name):
    return [s for family in families for s in family.samples if s.name == name]


# --- _parse_domain_stats ---


def test_parse_domain_stats_running_includes_all_fields():
    parsed = _parse_domain_stats(RAW_RUNNING)

    assert parsed["state"] == "running"
    assert parsed["is_running"] is True
    assert parsed["cpu_time_seconds"] == 12.3
    assert parsed["memory_current_bytes"] == 524288 * 1024
    assert parsed["memory_maximum_bytes"] == 1048576 * 1024
    assert parsed["memory_guest_total_bytes"] == 1000000 * 1024
    assert parsed["memory_guest_usable_bytes"] == 600000 * 1024
    assert parsed["vcpus"] == 2
    assert parsed["interfaces"] == [
        {
            "name": "vnet0",
            "rx_bytes": 100,
            "rx_packets": 10,
            "tx_bytes": 200,
            "tx_packets": 20,
        }
    ]
    assert parsed["disks"] == [
        {"name": "vda", "rd_bytes": 300, "rd_reqs": 30, "wr_bytes": 400, "wr_reqs": 40}
    ]


def test_parse_domain_stats_missing_device_name_falls_back_to_index():
    raw = dict(RAW_RUNNING)
    del raw["net.0.name"]
    del raw["block.0.name"]

    parsed = _parse_domain_stats(raw)

    assert parsed["interfaces"][0]["name"] == "net0"
    assert parsed["disks"][0]["name"] == "block0"


def test_parse_domain_stats_shutoff_has_no_resource_fields():
    raw = {"state.state": libvirt.VIR_DOMAIN_SHUTOFF, "state.reason": 1}

    parsed = _parse_domain_stats(raw)

    assert parsed["state"] == "shutoff"
    assert parsed["is_running"] is False
    assert parsed["cpu_time_seconds"] is None
    assert parsed["memory_current_bytes"] is None
    assert parsed["memory_maximum_bytes"] is None
    assert parsed["memory_guest_total_bytes"] is None
    assert parsed["memory_guest_usable_bytes"] is None
    assert parsed["vcpus"] is None
    assert parsed["interfaces"] == []
    assert parsed["disks"] == []


# --- DomainCollector.collect ---


def test_collect_only_includes_managed_domains():
    mgr = MagicMock()
    managed_dom = MagicMock()
    managed_dom.name.return_value = "web-1"
    unmanaged_dom = MagicMock()
    unmanaged_dom.name.return_value = "other-1"
    mgr.is_managed.side_effect = lambda dom: dom is managed_dom
    mgr.conn.getAllDomainStats.return_value = [
        (managed_dom, dict(RAW_RUNNING)),
        (unmanaged_dom, dict(RAW_RUNNING)),
    ]

    families = list(DomainCollector(lambda: mgr).collect())

    up_samples = _samples_by_name(families, "minivps_vm_up")
    assert [s.labels["vm"] for s in up_samples] == ["web-1"]


def test_collect_emits_one_hot_state():
    mgr = MagicMock()
    mgr.is_managed.return_value = True
    dom = MagicMock()
    dom.name.return_value = "web-1"
    mgr.conn.getAllDomainStats.return_value = [
        (dom, {"state.state": libvirt.VIR_DOMAIN_PAUSED})
    ]

    families = list(DomainCollector(lambda: mgr).collect())

    state_samples = {
        s.labels["state"]: s.value
        for s in _samples_by_name(families, "minivps_vm_state")
    }
    assert len(state_samples) == 8
    assert state_samples["paused"] == 1.0
    assert state_samples["running"] == 0.0


def test_collect_skips_resource_metrics_when_shutoff():
    mgr = MagicMock()
    mgr.is_managed.return_value = True
    dom = MagicMock()
    dom.name.return_value = "web-1"
    mgr.conn.getAllDomainStats.return_value = [
        (dom, {"state.state": libvirt.VIR_DOMAIN_SHUTOFF})
    ]

    families = list(DomainCollector(lambda: mgr).collect())

    assert _samples_by_name(families, "minivps_vm_up")[0].value == 0.0
    assert _samples_by_name(families, "minivps_vm_vcpus") == []
    assert _samples_by_name(families, "minivps_vm_cpu_seconds_total") == []


def test_collect_omits_guest_memory_when_balloon_driver_is_silent():
    """virtio_balloon が統計を報告しないゲストでは、ゲスト内メモリの系列を出さない。

    balloon.current/maximum は libvirt 側の値なので残る。
    """
    mgr = MagicMock()
    mgr.is_managed.return_value = True
    dom = MagicMock()
    dom.name.return_value = "web-1"
    raw = dict(RAW_RUNNING)
    del raw["balloon.available"]
    del raw["balloon.usable"]
    mgr.conn.getAllDomainStats.return_value = [(dom, raw)]

    families = list(DomainCollector(lambda: mgr).collect())

    assert _samples_by_name(families, "minivps_vm_memory_guest_total_bytes") == []
    assert _samples_by_name(families, "minivps_vm_memory_guest_usable_bytes") == []
    assert _samples_by_name(families, "minivps_vm_memory_current_bytes") != []


def test_collect_emits_metrics_per_device():
    mgr = MagicMock()
    mgr.is_managed.return_value = True
    dom = MagicMock()
    dom.name.return_value = "web-1"
    raw = dict(RAW_RUNNING)
    raw.update(
        {
            "net.count": 2,
            "net.1.name": "vnet1",
            "net.1.rx.bytes": 500,
            "net.1.rx.pkts": 5,
            "net.1.tx.bytes": 600,
            "net.1.tx.pkts": 6,
        }
    )
    mgr.conn.getAllDomainStats.return_value = [(dom, raw)]

    families = list(DomainCollector(lambda: mgr).collect())

    rx_samples = {
        s.labels["device"]: s.value
        for s in _samples_by_name(families, "minivps_vm_network_receive_bytes_total")
    }
    assert rx_samples == {"vnet0": 100, "vnet1": 500}

    disk_samples = {
        s.labels["device"]: s.value
        for s in _samples_by_name(families, "minivps_vm_disk_read_bytes_total")
    }
    assert disk_samples == {"vda": 300}


# --- エラーハンドリング ---


def test_collect_reports_scrape_success_when_healthy():
    mgr = MagicMock()
    mgr.is_managed.return_value = True
    dom = MagicMock()
    dom.name.return_value = "web-1"
    mgr.conn.getAllDomainStats.return_value = [(dom, dict(RAW_RUNNING))]

    families = list(DomainCollector(lambda: mgr).collect())

    success = _samples_by_name(families, "minivps_exporter_scrape_success")
    assert [s.value for s in success] == [1.0]


def test_collect_survives_libvirt_failure_and_reconnects():
    broken_mgr = MagicMock()
    broken_mgr.conn.getAllDomainStats.side_effect = libvirt.libvirtError("down")
    healthy_mgr = MagicMock()
    healthy_mgr.is_managed.return_value = True
    dom = MagicMock()
    dom.name.return_value = "web-1"
    healthy_mgr.conn.getAllDomainStats.return_value = [(dom, dict(RAW_RUNNING))]
    factory = MagicMock(side_effect=[broken_mgr, healthy_mgr])
    collector = DomainCollector(factory)

    failed = list(collector.collect())

    # 失敗時: 例外を伝播させず scrape_success=0 のみ、VM メトリクスは無い
    success = _samples_by_name(failed, "minivps_exporter_scrape_success")
    assert [s.value for s in success] == [0.0]
    assert _samples_by_name(failed, "minivps_vm_up") == []
    broken_mgr.conn.close.assert_called_once()

    recovered = list(collector.collect())

    # 次回スクレイプ: factory から再接続して復旧する
    assert factory.call_count == 2
    success = _samples_by_name(recovered, "minivps_exporter_scrape_success")
    assert [s.value for s in success] == [1.0]
    up_samples = _samples_by_name(recovered, "minivps_vm_up")
    assert [s.labels["vm"] for s in up_samples] == ["web-1"]


def test_collect_warns_on_libvirt_failure(caplog):
    """scrape_success=0 だけでは理由が分からないため、原因を WARNING に残す。"""
    broken_mgr = MagicMock()
    broken_mgr.conn.getAllDomainStats.side_effect = libvirt.libvirtError("down")
    collector = DomainCollector(MagicMock(return_value=broken_mgr))

    with caplog.at_level("WARNING", logger="mini_vps.exporter"):
        list(collector.collect())

    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    assert "統計の取得に失敗" in caplog.records[0].getMessage()


def test_collect_skips_domain_vanished_mid_scrape():
    mgr = MagicMock()
    vanished_dom = MagicMock()
    alive_dom = MagicMock()
    alive_dom.name.return_value = "web-2"

    def is_managed(dom):
        if dom is vanished_dom:
            raise libvirt.libvirtError("domain not found")
        return True

    mgr.is_managed.side_effect = is_managed
    mgr.conn.getAllDomainStats.return_value = [
        (vanished_dom, dict(RAW_RUNNING)),
        (alive_dom, dict(RAW_RUNNING)),
    ]

    families = list(DomainCollector(lambda: mgr).collect())

    # 消えた 1 台だけスキップし、残りとスクレイプ自体は成功扱い
    up_samples = _samples_by_name(families, "minivps_vm_up")
    assert [s.labels["vm"] for s in up_samples] == ["web-2"]
    success = _samples_by_name(families, "minivps_exporter_scrape_success")
    assert [s.value for s in success] == [1.0]


def test_main_configures_logging(monkeypatch):
    """入口層としてのログ設定が main() で行われることを確認する。"""
    captured = []
    monkeypatch.setattr(
        "mini_vps.exporter.configure_logging", lambda *a, **kw: captured.append(True)
    )
    monkeypatch.setattr("mini_vps.exporter.register_quiet_error_handler", lambda: None)
    monkeypatch.setattr("mini_vps.exporter.REGISTRY.register", lambda c: None)
    monkeypatch.setattr("mini_vps.exporter.start_http_server", lambda port, addr: None)
    # main() は最後に永久待機するため、待機だけ即返るよう差し替える。
    monkeypatch.setattr("mini_vps.exporter.threading.Event", lambda: MagicMock())

    main()

    assert captured == [True]


# --- ホスト全体のメトリクス ---


def test_parse_host_stats_sums_allocations_and_pools():
    specs = {
        "web-1": {"memory": 1024, "vcpus": 2},
        "db-1": {"memory": 4096, "vcpus": 4},
    }
    pools = {"vps-pool": [2, 1000, 400, 600], "images": [2, 1000, 300, 700]}

    host = _parse_host_stats(["x86_64", 8192, 4, 2100, 1, 1, 4, 1], specs, pools)

    assert host == {
        "memory_bytes": 8192 * 1024 * 1024,
        "cpus": 4,
        "allocated_memory_bytes": 5120 * 1024 * 1024,
        "allocated_vcpus": 6,
        "pools": {
            "vps-pool": {
                "capacity_bytes": 1000,
                "allocation_bytes": 400,
                "available_bytes": 600,
            },
            "images": {
                "capacity_bytes": 1000,
                "allocation_bytes": 300,
                "available_bytes": 700,
            },
        },
    }


def test_parse_host_stats_without_vms_or_pools():
    host = _parse_host_stats(["x86_64", 2048, 2, 0, 1, 1, 2, 1], {}, {})

    assert host["allocated_memory_bytes"] == 0
    assert host["allocated_vcpus"] == 0
    assert host["pools"] == {}


def _host_mgr():
    mgr = MagicMock()
    mgr.conn.getAllDomainStats.return_value = []
    mgr.conn.getInfo.return_value = ["x86_64", 8192, 4, 2100, 1, 1, 4, 1]
    mgr.managed_specs.return_value = {"web-1": {"memory": 1024, "vcpus": 2}}
    pools = {}
    for name in ("vps-pool", "vps-seeds", "images", "default"):
        pool = MagicMock()
        pool.name.return_value = name
        pool.info.return_value = [2, 1000, 400, 600]
        pools[name] = pool
    mgr.conn.listAllStoragePools.return_value = [
        pools["vps-pool"],
        pools["images"],
        pools["default"],
    ]
    mgr.conn.storagePoolLookupByName.side_effect = lambda n: pools[n]
    return mgr


def test_collect_emits_host_metrics():
    families = list(DomainCollector(_host_mgr).collect())

    def value(name):
        return _samples_by_name(families, name)[0].value

    assert value("minivps_host_memory_bytes") == 8192 * 1024 * 1024
    assert value("minivps_host_cpus") == 4
    assert value("minivps_allocated_memory_bytes") == 1024 * 1024 * 1024
    assert value("minivps_allocated_vcpus") == 2
    # 存在するプールのうち minivps が使う3つだけを出す(無い vps-seeds は出さない)。
    available = {
        s.labels["pool"]: s.value
        for s in _samples_by_name(families, "minivps_pool_available_bytes")
    }
    assert available == {"vps-pool": 600, "images": 600}
    assert _samples_by_name(families, "minivps_pool_capacity_bytes")[0].value == 1000
    assert _samples_by_name(families, "minivps_pool_allocation_bytes")[0].value == 400


def test_collect_marks_scrape_failed_when_host_stats_fail():
    mgr = _host_mgr()
    mgr.conn.getInfo.side_effect = libvirt.libvirtError("connection lost")

    families = list(DomainCollector(lambda: mgr).collect())

    success = _samples_by_name(families, "minivps_exporter_scrape_success")
    assert [s.value for s in success] == [0.0]
    assert _samples_by_name(families, "minivps_host_memory_bytes") == []
