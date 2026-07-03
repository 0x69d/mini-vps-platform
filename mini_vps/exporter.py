"""管理対象 VM の libvirt 統計を Prometheus 形式で公開するエクスポーター。

`conn.getAllDomainStats()` の一括統計 API から取得した生データを正規化し、
`prometheus_client` の Custom Collector として公開する。独立プロセスとして
`uv run python -m mini_vps.exporter` で起動し、Prometheus サーバーからの
pull を待ち受ける。
"""

import os
import threading

import libvirt
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from .config import LIBVIRT_URI
from .manager import STATE_NAMES, ServerManager

_DEFAULT_PORT = 9177
_PORT_ENV_VAR = "MINIVPS_EXPORTER_PORT"
# 単一ホスト内完結が前提(README「単一ホスト上でローカル完結」)のため、
# 既定では外部interfaceに公開しない。
_DEFAULT_ADDR = "127.0.0.1"
_ADDR_ENV_VAR = "MINIVPS_EXPORTER_ADDR"


def _parse_domain_stats(raw: dict) -> dict:
    """getAllDomainStats() が返す1ドメイン分の生 dict を正規化する。

    停止中(shutoff)ドメインは state 以外のキーがほとんど入らないため、
    全フィールドを `.get()` で取り出し、欠損時は None または空 list で返す。

    Args:
        raw: virConnect.getAllDomainStats() が返す typed parameter dict。

    Returns:
        state・is_running・cpu_time_seconds・memory_current_bytes・
        memory_maximum_bytes・interfaces・disks をキーに持つ dict。
    """
    state_code = raw.get("state.state")
    cpu_time_ns = raw.get("cpu.time")
    balloon_current_kib = raw.get("balloon.current")
    balloon_maximum_kib = raw.get("balloon.maximum")

    interfaces = [
        {
            "name": raw.get(f"net.{i}.name", f"net{i}"),
            "rx_bytes": raw.get(f"net.{i}.rx.bytes", 0),
            "rx_packets": raw.get(f"net.{i}.rx.pkts", 0),
            "tx_bytes": raw.get(f"net.{i}.tx.bytes", 0),
            "tx_packets": raw.get(f"net.{i}.tx.pkts", 0),
        }
        for i in range(raw.get("net.count", 0))
    ]
    disks = [
        {
            "name": raw.get(f"block.{i}.name", f"block{i}"),
            "rd_bytes": raw.get(f"block.{i}.rd.bytes", 0),
            "rd_reqs": raw.get(f"block.{i}.rd.reqs", 0),
            "wr_bytes": raw.get(f"block.{i}.wr.bytes", 0),
            "wr_reqs": raw.get(f"block.{i}.wr.reqs", 0),
        }
        for i in range(raw.get("block.count", 0))
    ]

    return {
        "state": STATE_NAMES.get(state_code, "unknown"),
        "is_running": state_code == libvirt.VIR_DOMAIN_RUNNING,
        "cpu_time_seconds": cpu_time_ns / 1e9 if cpu_time_ns is not None else None,
        "memory_current_bytes": (
            balloon_current_kib * 1024 if balloon_current_kib is not None else None
        ),
        "memory_maximum_bytes": (
            balloon_maximum_kib * 1024 if balloon_maximum_kib is not None else None
        ),
        "vcpus": raw.get("vcpu.current"),
        "interfaces": interfaces,
        "disks": disks,
    }


class DomainCollector:
    """管理対象 VM の統計を Prometheus メトリクスとして公開する Collector。"""

    def __init__(self, mgr: ServerManager):
        self._mgr = mgr

    def collect(self):
        """管理対象 VM ごとのメトリクスファミリーを生成する。

        「どの domain が管理対象か」の判定は ServerManager.is_managed() に一元化し、
        getAllDomainStats() の結果を domain ごとに直接フィルタする(list() による
        事前の全件列挙を挟まないことで、二重列挙とその間の TOCTOU を避ける)。

        Yields:
            prometheus_client の MetricFamily。
        """
        up = GaugeMetricFamily(
            "minivps_vm_up", "1 if the VM is running, 0 otherwise", labels=["vm"]
        )
        state = GaugeMetricFamily(
            "minivps_vm_state",
            "1 for the VM's current state, 0 for the others",
            labels=["vm", "state"],
        )
        vcpus = GaugeMetricFamily(
            "minivps_vm_vcpus", "Number of current vCPUs", labels=["vm"]
        )
        mem_current = GaugeMetricFamily(
            "minivps_vm_memory_current_bytes", "Current memory in bytes", labels=["vm"]
        )
        mem_maximum = GaugeMetricFamily(
            "minivps_vm_memory_maximum_bytes", "Maximum memory in bytes", labels=["vm"]
        )
        cpu_seconds = CounterMetricFamily(
            "minivps_vm_cpu_seconds", "Cumulative CPU time in seconds", labels=["vm"]
        )
        net_rx_bytes = CounterMetricFamily(
            "minivps_vm_network_receive_bytes",
            "Received bytes",
            labels=["vm", "device"],
        )
        net_tx_bytes = CounterMetricFamily(
            "minivps_vm_network_transmit_bytes",
            "Transmitted bytes",
            labels=["vm", "device"],
        )
        net_rx_packets = CounterMetricFamily(
            "minivps_vm_network_receive_packets",
            "Received packets",
            labels=["vm", "device"],
        )
        net_tx_packets = CounterMetricFamily(
            "minivps_vm_network_transmit_packets",
            "Transmitted packets",
            labels=["vm", "device"],
        )
        disk_rd_bytes = CounterMetricFamily(
            "minivps_vm_disk_read_bytes",
            "Bytes read from disk",
            labels=["vm", "device"],
        )
        disk_wr_bytes = CounterMetricFamily(
            "minivps_vm_disk_write_bytes",
            "Bytes written to disk",
            labels=["vm", "device"],
        )
        disk_rd_requests = CounterMetricFamily(
            "minivps_vm_disk_read_requests",
            "Read requests to disk",
            labels=["vm", "device"],
        )
        disk_wr_requests = CounterMetricFamily(
            "minivps_vm_disk_write_requests",
            "Write requests to disk",
            labels=["vm", "device"],
        )

        for dom, raw in self._mgr.conn.getAllDomainStats():
            if not self._mgr.is_managed(dom):
                continue
            name = dom.name()

            parsed = _parse_domain_stats(raw)

            up.add_metric([name], 1.0 if parsed["is_running"] else 0.0)
            for state_name in STATE_NAMES.values():
                state.add_metric(
                    [name, state_name], 1.0 if state_name == parsed["state"] else 0.0
                )
            if parsed["vcpus"] is not None:
                vcpus.add_metric([name], parsed["vcpus"])
            if parsed["memory_current_bytes"] is not None:
                mem_current.add_metric([name], parsed["memory_current_bytes"])
            if parsed["memory_maximum_bytes"] is not None:
                mem_maximum.add_metric([name], parsed["memory_maximum_bytes"])
            if parsed["cpu_time_seconds"] is not None:
                cpu_seconds.add_metric([name], parsed["cpu_time_seconds"])
            for iface in parsed["interfaces"]:
                labels = [name, iface["name"]]
                net_rx_bytes.add_metric(labels, iface["rx_bytes"])
                net_tx_bytes.add_metric(labels, iface["tx_bytes"])
                net_rx_packets.add_metric(labels, iface["rx_packets"])
                net_tx_packets.add_metric(labels, iface["tx_packets"])
            for disk in parsed["disks"]:
                labels = [name, disk["name"]]
                disk_rd_bytes.add_metric(labels, disk["rd_bytes"])
                disk_wr_bytes.add_metric(labels, disk["wr_bytes"])
                disk_rd_requests.add_metric(labels, disk["rd_reqs"])
                disk_wr_requests.add_metric(labels, disk["wr_reqs"])

        yield up
        yield state
        yield vcpus
        yield mem_current
        yield mem_maximum
        yield cpu_seconds
        yield net_rx_bytes
        yield net_tx_bytes
        yield net_rx_packets
        yield net_tx_packets
        yield disk_rd_bytes
        yield disk_wr_bytes
        yield disk_rd_requests
        yield disk_wr_requests


def main() -> None:
    """Prometheus エクスポーターを起動する。"""
    port = int(os.environ.get(_PORT_ENV_VAR, _DEFAULT_PORT))
    addr = os.environ.get(_ADDR_ENV_VAR, _DEFAULT_ADDR)

    conn = libvirt.open(LIBVIRT_URI)
    mgr = ServerManager(conn)
    REGISTRY.register(DomainCollector(mgr))

    start_http_server(port, addr=addr)
    threading.Event().wait()


if __name__ == "__main__":
    main()
