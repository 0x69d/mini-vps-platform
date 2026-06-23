import libvirt

from .lifecycle import provision, teardown, wait_for_ip
from .spec import SAMPLE_SPEC, load_spec


def main():
    conn = libvirt.open("qemu:///system")

    spec = load_spec(SAMPLE_SPEC)
    print(f"spec: {spec}")

    print("\n=== 0. 前回の残骸を消す ===")
    teardown(conn, spec)

    print("\n=== 1-5. スペックから一気通貫でプロビジョニング ===")
    dom = provision(conn, spec)

    print("\n=== 6. DHCPでIPを取る ===")
    ip = wait_for_ip(dom)
    print(f"  IP: {ip}")
    print(f"  ssh {spec.get('user', 'ubuntu')}@{ip} # パスワード無しで入れるはず！")

    # print("\n=== 7. 後始末 ===")
    # teardown(conn, spec)

    conn.close()


if __name__ == "__main__":
    main()
