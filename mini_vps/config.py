"""リソースの定数と XML テンプレート群。"""

LIBVIRT_URI = "qemu:///system"
POOL_NAME = "vps-pool"
POOL_PATH = "/var/lib/libvirt/vps-pool"
BASE_POOL = "images"
SEED_DIR = "/var/lib/libvirt/seeds"
SEED_POOL_NAME = "vps-seeds"

# 管理対象 domain の <metadata> に spec を埋め込むための名前空間。
# URI は単なる一意識別子で、機能上は任意の文字列でよい(プレースホルダ)。
METADATA_NS = "https://example.org/minivps"
METADATA_KEY = "minivps"
POOL_XML = f"""
<pool type='dir'>
  <name>{POOL_NAME}</name>
  <target>
    <path>{POOL_PATH}</path>
  </target>
</pool>
"""

SEED_POOL_XML = f"""
<pool type='dir'>
  <name>{SEED_POOL_NAME}</name>
  <target>
    <path>{SEED_DIR}</path>
  </target>
</pool>
"""

OVERLAY_VOL_XML_TEMPLATE = """
<volume>
  <name>{name}.qcow2</name>
  <capacity unit='GiB'>{disk}</capacity>
  <target>
    <format type='qcow2'/>
  </target>
  <backingStore>
    <path>{base_path}</path>
    <format type='qcow2'/>
  </backingStore>
</volume>
"""

SEED_VOL_XML_TEMPLATE = """
<volume>
  <name>{name}</name>
  <capacity unit='bytes'>{capacity_bytes}</capacity>
  <target>
    <format type='raw'/>
  </target>
</volume>
"""

META_DATA_TEMPLATE = """\
instance-id: iid-{name}-001
local-hostname: {hostname}
"""

DOMAIN_XML_TEMPLATE = """
<domain type='kvm'>
  <name>{name}</name>
  <memory unit='KiB'>{memory_kib}</memory>
  <vcpu>{vcpus}</vcpu>
  <cpu mode='host-model'/>
  <os firmware='efi'>
    <type arch='x86_64' machine='q35'>hvm</type>
    <loader secure='no'/>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
  </features>
  <clock offset='utc'/>
  <pm>
    <suspend-to-mem enabled='no'/>
    <suspend-to-disk enabled='no'/>
  </pm>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' discard='unmap'/>
      <source file='{overlay_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{seed_path}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
{interfaces}\
    <rng model='virtio'>
      <backend model='random'>/dev/urandom</backend>
    </rng>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
  </devices>
</domain>
"""

# VM 1台につき spec["networks"] の要素数だけ連結して <devices> に埋め込む。
# str.format はブロックの繰り返し生成ができないため、DOMAIN_XML_TEMPLATE から
# <interface> 部分だけを分離している。
INTERFACE_XML_TEMPLATE = """\
    <interface type='network'>
      <mac address='{mac}'/>
      <source network='{network}'/>
      <model type='virtio'/>
      {filterref}
    </interface>
"""

# 宣言ポート1件分の accept ルール。protocol("tcp"/"udp")に応じてタグ名を差し替える。
NWFILTER_PORT_RULE_TEMPLATE = """\
  <rule action='accept' direction='in' priority='500'>
    <{protocol} dstportstart='{port}'/>
  </rule>
"""

# ESTABLISHED,RELATED の accept が無いと、VM 自身が発信した通信(DNS/apt 等)への
# 応答まで default drop に落ちる。nwfilter は記述順ではなく priority 昇順で評価される
# ため、default drop には他より大きい priority を明示する必要がある。
NWFILTER_XML_TEMPLATE = """
<filter name='{name}' chain='root'>
  <filterref filter='allow-arp'/>
  <filterref filter='allow-dhcp'/>
  <rule action='accept' direction='in' priority='500'>
    <all state='ESTABLISHED,RELATED'/>
  </rule>
{port_rules}\
  <rule action='accept' direction='out' priority='500'>
    <all/>
  </rule>
  <rule action='drop' direction='in' priority='1000'>
    <all/>
  </rule>
</filter>
"""

# spec["static_routes"] をゲスト起動時に永続適用するための systemd oneshot ユニット。
# runcmd(cloud-init 初回起動時のみ実行)だけでは再起動後にルートが消えるため、
# systemctl enable でブートのたびに再適用する形にしている。
STATIC_ROUTES_UNIT_NAME = "minivps-static-routes.service"
STATIC_ROUTES_UNIT_PATH = f"/etc/systemd/system/{STATIC_ROUTES_UNIT_NAME}"

STATIC_ROUTES_UNIT_TEMPLATE = """\
[Unit]
Description=mini-vps-platform static routes (managed, do not edit)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
{exec_lines}

[Install]
WantedBy=multi-user.target
"""

# ExecStart 1行分。先頭の "-" は、この経路の via が到達不能で失敗しても他の
# ExecStart 行の適用を止めないためのもの(失敗はユニット全体のステータスには
# 現れなくなるため、確認には journalctl -u が必要)。
STATIC_ROUTES_EXEC_LINE_TEMPLATE = "ExecStart=-ip route replace {destination} via {via}"
