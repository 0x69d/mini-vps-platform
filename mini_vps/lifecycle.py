import os
import time

import libvirt

from .config import LAB_DIR, POOL_NAME
from .resources import build_domain_xml, build_seed_iso, create_overlay_volume
from .spec import read_pubkey


def provision(conn, spec) -> "libvirt.virDomain":
    """
    spec から VM を一気通貫で作る: overlay → seed → domain XML → defineXML → create。Domain を返す。
    """
    net = conn.networkLookupByName(spec.get("network", "default"))

    if not net.isActive():
        net.create()

    overlay_path = create_overlay_volume(conn, spec)
    seed_path = build_seed_iso(spec, read_pubkey())
    xml = build_domain_xml(spec, overlay_path, seed_path)
    dom = conn.defineXML(xml)
    dom.create()
    return dom


def wait_for_ip(dom: "libvirt.virDomain", timeout=120) -> str | None:
    """
    dom の DHCP リースを直接引いて IPv4 を返す。タイムアウトで None。
    libvirt が dom の NIC(MAC) に紐づくリースだけを返すため、古いリースを掴まない。
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        ifaces = dom.interfaceAddresses(
            libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE
        )
        for iface in ifaces.values():
            for addr in iface["addrs"]:
                if addr["type"] == libvirt.VIR_IP_ADDR_TYPE_IPV4:
                    return addr["addr"]
        time.sleep(2)
    return None


def teardown(conn, spec) -> None:
    """
    spec の VM を後始末: destroy → undefine → overlay を名前指定で削除 → seed 削除。
    """
    # domain
    if spec["name"] in {d.name() for d in conn.listAllDomains()}:
        dom = conn.lookupByName(spec["name"])
        if dom.isActive():
            dom.destroy()
        dom.undefine()

    # overlay volume
    vol_name = f"{spec['name']}.qcow2"
    if POOL_NAME in {p.name() for p in conn.listAllStoragePools()}:
        pool = conn.storagePoolLookupByName(POOL_NAME)
        if vol_name in {v.name() for v in pool.listAllVolumes()}:
            pool.storageVolLookupByName(vol_name).delete(0)

    # seed
    seed_path = f"{LAB_DIR}/{spec['name']}-seed.iso"
    if os.path.exists(seed_path):
        os.remove(seed_path)
