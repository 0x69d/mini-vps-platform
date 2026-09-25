"""管理対象 VM の libvirt 統計を Prometheus 形式で公開するエクスポーター。

`conn.getAllDomainStats()` の一括統計 API から取得した生データを正規化し、
`prometheus_client` の Custom Collector として公開する。独立プロセスとして
`uv run python -m mini_vps.exporter` で起動し、Prometheus サーバーからの
pull を待ち受ける。
"""

import logging
import os
import threading

import libvirt
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from .config import BASE_POOL, POOL_NAME, SEED_POOL_NAME
from .logging_config import configure as configure_logging
from .manager import STATE_NAMES, ServerManager, register_quiet_error_handler
from .platform_profile import get_profile

_LOGGER = logging.getLogger(__name__)

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
    """
    state_code = raw.get("state.state")
    cpu_time_ns = raw.get("cpu.time")
    balloon_current_kib = raw.get("balloon.current")
    balloon_maximum_kib = raw.get("balloon.maximum")
    # balloon.available/usable はゲストの virtio_balloon ドライバが報告する値で、
    # domain XML の <memballoon><stats period> が無いと更新されない。
    # ドライバを持たないゲストでは欠損するため他のフィールドと同様に None を許す。
    balloon_available_kib = raw.get("balloon.available")
    balloon_usable_kib = raw.get("balloon.usable")

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
        "memory_guest_total_bytes": (
            balloon_available_kib * 1024 if balloon_available_kib is not None else None
        ),
        "memory_guest_usable_bytes": (
            balloon_usable_kib * 1024 if balloon_usable_kib is not None else None
        ),
        "vcpus": raw.get("vcpu.current"),
        "interfaces": interfaces,
        "disks": disks,
    }


# ホスト容量として公開するストレージプール(overlay・seed・base image)。
HOST_POOLS = (POOL_NAME, SEED_POOL_NAME, BASE_POOL)

_MIB = 1024 * 1024


def _parse_host_stats(
    node_info: list, specs: dict[str, dict], pool_infos: dict[str, list]
) -> dict:
    """ホスト全体の容量と割当を正規化する(純粋関数)。

    割当は管理対象 VM の spec(metadata)の memory/vcpus を稼働状態に関わらず
    合計する。admission.py の容量チェックと同じ数え方で、autostart で全台が同時に
    起きたときに必要になる量を表す。

    Args:
        node_info: `conn.getInfo()` の戻り値 [model, memory(MiB), cpus, ...]。
        specs: 管理対象 VM の name → spec(ServerManager.managed_specs())。
        pool_infos: プール名 → `pool.info()` の戻り値
            [state, capacity, allocation, available](バイト)。無いプールは含めない。

    Returns:
        memory_bytes / cpus / allocated_memory_bytes / allocated_vcpus / pools
        (プール名 → capacity_bytes・allocation_bytes・available_bytes)を持つ dict。
    """
    return {
        "memory_bytes": node_info[1] * _MIB,
        "cpus": node_info[2],
        "allocated_memory_bytes": sum(s.get("memory", 0) for s in specs.values())
        * _MIB,
        "allocated_vcpus": sum(s.get("vcpus", 0) for s in specs.values()),
        "pools": {
            name: {
                "capacity_bytes": info[1],
                "allocation_bytes": info[2],
                "available_bytes": info[3],
            }
            for name, info in pool_infos.items()
        },
    }


def _collect_host_stats(mgr: ServerManager) -> dict:
    """ホスト全体の統計を libvirt から集めて _parse_host_stats に渡す。"""
    conn = mgr.conn
    existing = {p.name() for p in conn.listAllStoragePools()}
    pool_infos = {
        name: conn.storagePoolLookupByName(name).info()
        for name in HOST_POOLS
        if name in existing
    }
    return _parse_host_stats(conn.getInfo(), mgr.managed_specs(), pool_infos)


def _default_manager_factory() -> ServerManager:
    """既定の接続先(HostProfile.libvirt_uri)に接続した ServerManager を生成する。"""
    return ServerManager(libvirt.open(get_profile().libvirt_uri))


class DomainCollector:
    """管理対象 VM の統計を Prometheus メトリクスとして公開する Collector。

    libvirt 接続はスクレイプ時に遅延生成し、libvirtError 発生時は接続を
    破棄して次回スクレイプで再接続する(libvirtd 再起動からの自動復旧)。
    """

    def __init__(self, manager_factory=_default_manager_factory):
        self._manager_factory = manager_factory
        self._mgr: ServerManager | None = None

    def _drop_manager(self) -> None:
        """壊れた可能性のある接続を破棄し、次回スクレイプで再接続させる。"""
        if self._mgr is not None:
            try:
                self._mgr.conn.close()
            except libvirt.libvirtError:
                pass
            self._mgr = None

    def collect(self):
        """管理対象 VM ごとのメトリクスファミリーを生成する。

        「どの domain が管理対象か」の判定は ServerManager.is_managed() に一元化し、
        getAllDomainStats() の結果を domain ごとに直接フィルタする(list() による
        事前の全件列挙を挟まないことで、二重列挙とその間の TOCTOU を避ける)。

        あわせてホスト全体の容量(メモリ・論理 CPU・プール)と管理対象 VM の割当の
        合計を出す(_parse_host_stats 参照)。

        libvirt との通信に失敗した場合は例外を伝播させず、
        `minivps_exporter_scrape_success` を 0 にして VM メトリクスを出さない
        (Prometheus 側で `absent()` や `scrape_success == 0` の条件が書ける)。
        スクレイプ中に消えた domain は 1 台単位でスキップする。
        """
        scrape_success = GaugeMetricFamily(
            "minivps_exporter_scrape_success",
            "1 if the last scrape of libvirt succeeded, 0 otherwise",
        )
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
        mem_guest_total = GaugeMetricFamily(
            "minivps_vm_memory_guest_total_bytes",
            "Total memory seen by the guest in bytes (libvirt balloon.available)",
            labels=["vm"],
        )
        mem_guest_usable = GaugeMetricFamily(
            "minivps_vm_memory_guest_usable_bytes",
            "Memory allocatable without swapping in the guest in bytes "
            "(libvirt balloon.usable)",
            labels=["vm"],
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

        host_memory = GaugeMetricFamily(
            "minivps_host_memory_bytes", "Physical memory of the host in bytes"
        )
        host_cpus = GaugeMetricFamily(
            "minivps_host_cpus", "Number of logical CPUs of the host"
        )
        allocated_memory = GaugeMetricFamily(
            "minivps_allocated_memory_bytes",
            "Sum of memory declared by managed VMs (running or not) in bytes",
        )
        allocated_vcpus = GaugeMetricFamily(
            "minivps_allocated_vcpus",
            "Sum of vCPUs declared by managed VMs (running or not)",
        )
        pool_capacity = GaugeMetricFamily(
            "minivps_pool_capacity_bytes",
            "Capacity of the storage pool in bytes",
            labels=["pool"],
        )
        pool_allocation = GaugeMetricFamily(
            "minivps_pool_allocation_bytes",
            "Allocation of the storage pool in bytes",
            labels=["pool"],
        )
        pool_available = GaugeMetricFamily(
            "minivps_pool_available_bytes",
            "Free space of the storage pool in bytes",
            labels=["pool"],
        )

        try:
            if self._mgr is None:
                self._mgr = self._manager_factory()
            all_stats = self._mgr.conn.getAllDomainStats()
            host = _collect_host_stats(self._mgr)
        except libvirt.libvirtError as e:
            _LOGGER.warning("統計の取得に失敗、接続を張り直す: %s", e)
            self._drop_manager()
            scrape_success.add_metric([], 0.0)
            yield scrape_success
            return

        for dom, raw in all_stats:
            # getAllDomainStats() 取得後に delete された domain への
            # metadata()/name() は失敗するため、その 1 台だけスキップする。
            try:
                if not self._mgr.is_managed(dom):
                    continue
                name = dom.name()
            except libvirt.libvirtError:
                continue

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
            if parsed["memory_guest_total_bytes"] is not None:
                mem_guest_total.add_metric([name], parsed["memory_guest_total_bytes"])
            if parsed["memory_guest_usable_bytes"] is not None:
                mem_guest_usable.add_metric([name], parsed["memory_guest_usable_bytes"])
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

        host_memory.add_metric([], host["memory_bytes"])
        host_cpus.add_metric([], host["cpus"])
        allocated_memory.add_metric([], host["allocated_memory_bytes"])
        allocated_vcpus.add_metric([], host["allocated_vcpus"])
        for pool_name, pool in host["pools"].items():
            pool_capacity.add_metric([pool_name], pool["capacity_bytes"])
            pool_allocation.add_metric([pool_name], pool["allocation_bytes"])
            pool_available.add_metric([pool_name], pool["available_bytes"])

        scrape_success.add_metric([], 1.0)
        yield scrape_success
        yield host_memory
        yield host_cpus
        yield allocated_memory
        yield allocated_vcpus
        yield pool_capacity
        yield pool_allocation
        yield pool_available
        yield up
        yield state
        yield vcpus
        yield mem_current
        yield mem_maximum
        yield mem_guest_total
        yield mem_guest_usable
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

    configure_logging()
    register_quiet_error_handler()
    REGISTRY.register(DomainCollector())

    start_http_server(port, addr=addr)
    _LOGGER.info("エクスポーターを起動した %s:%d", addr, port)
    threading.Event().wait()


if __name__ == "__main__":
    main()
