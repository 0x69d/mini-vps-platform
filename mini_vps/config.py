"""リソースの定数と XML テンプレート群。"""

LIBVIRT_URI = "qemu:///system"
POOL_NAME = "vps-pool"
POOL_PATH = "/var/lib/libvirt/images/vps-pool"
BASE_POOL = "images"
LAB_DIR = "/var/lib/libvirt/images/lab"

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

USER_DATA_TEMPLATE = """\
#cloud-config
hostname: {hostname}
users:
  - name: {user}
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - {pubkey}
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
  <os>
    <type arch='x86_64'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
  </features>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{overlay_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{seed_path}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
    <interface type='network'>
      <source network='{network}'/>
      <model type='virtio'/>
      {filterref}
    </interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
  </devices>
</domain>
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
