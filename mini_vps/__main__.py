"""デモスクリプト。"""

import libvirt

from .lifecycle import teardown, wait_for_ip
from .manager import ServerManager
from .spec import SAMPLE_SPEC, load_spec


def main():
    """デモシーケンスを実行する。"""
    conn = libvirt.open("qemu:///system")
    mgr = ServerManager(conn)

    spec = load_spec(SAMPLE_SPEC)
    name = spec["name"]
    print(f"spec: {spec}")

    print("\n=== 0. 前回の残骸を消す ===")
    teardown(conn, {"name": name})

    print("\n=== 1. spec から VM を作成(spec を metadata に埋め込む) ===")
    mgr.create(spec)

    print("\n=== 2. 管理対象として一覧に現れる ===")
    print(f"  servers: {mgr.list()}")

    print("\n=== 3. name から spec を往復で復元 + 状態取得 ===")
    print(f"  get: {mgr.get(name)}")

    print("\n=== 4. DHCPでIPを取る ===")
    dom = conn.lookupByName(name)
    ip = wait_for_ip(dom)
    print(f"  IP: {ip}")
    print(f"  ssh {spec['user']}@{ip} # パスワード無しで入れるはず！")

    # print("\n=== 5. 後始末 ===")
    # mgr.delete(name)

    conn.close()


if __name__ == "__main__":
    main()
