from unittest.mock import MagicMock

import libvirt

from mini_vps.exporter import DomainCollector, _parse_domain_stats

RAW_RUNNING = {
    "state.state": libvirt.VIR_DOMAIN_RUNNING,
    "state.reason": 1,
    "cpu.time": 12_300_000_000,
    "balloon.current": 524288,
    "balloon.maximum": 1048576,
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

    families = list(DomainCollector(mgr).collect())

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

    families = list(DomainCollector(mgr).collect())

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

    families = list(DomainCollector(mgr).collect())

    assert _samples_by_name(families, "minivps_vm_up")[0].value == 0.0
    assert _samples_by_name(families, "minivps_vm_vcpus") == []
    assert _samples_by_name(families, "minivps_vm_cpu_seconds_total") == []


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

    families = list(DomainCollector(mgr).collect())

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
