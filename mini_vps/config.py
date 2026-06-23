POOL_NAME = "vps-pool"
POOL_PATH = "/var/lib/libvirt/images/vps-pool"
BASE_POOL = "images"
LAB_DIR = "/var/lib/libvirt/images/lab"
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
    </interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
  </devices>
</domain>
"""
