import pytest

from mini_vps.platform_profile import (
    NETWORK_LIBVIRT,
    NETWORK_USER,
    detect,
    get_profile,
    set_profile,
)


def test_detect_linux_with_kvm():
    profile = detect(system="Linux", machine="x86_64", env={}, kvm_available=True)
    assert profile.libvirt_uri == "qemu:///system"
    assert (profile.accel, profile.domain_type) == ("kvm", "kvm")
    assert (profile.arch, profile.machine) == ("x86_64", "q35")
    assert profile.cpu_mode == "host-model"
    assert profile.network_mode == NETWORK_LIBVIRT
    assert profile.supports_nwfilter is True
    assert (profile.disk_cache, profile.disk_io) == ("none", "native")
    assert profile.pool_path == "/var/lib/libvirt/vps-pool"
    assert profile.lock_dir == "/run/minivps/locks"


def test_detect_linux_without_kvm_falls_back_to_tcg():
    profile = detect(system="Linux", machine="x86_64", env={}, kvm_available=False)
    assert (profile.accel, profile.domain_type, profile.cpu_mode) == (
        "tcg",
        "qemu",
        "maximum",
    )


def test_detect_linux_aarch64_uses_virt_machine():
    profile = detect(system="Linux", machine="aarch64", env={}, kvm_available=True)
    assert (profile.arch, profile.machine) == ("aarch64", "virt")


def test_detect_macos_apple_silicon():
    profile = detect(system="Darwin", machine="arm64", env={"HOME": "/Users/u"})
    assert profile.libvirt_uri == "qemu:///session"
    assert (profile.accel, profile.domain_type) == ("hvf", "hvf")
    assert (profile.arch, profile.machine) == ("aarch64", "virt")
    assert profile.cpu_mode == "host-passthrough"
    assert profile.network_mode == NETWORK_USER
    assert profile.supports_nwfilter is False
    assert (profile.disk_cache, profile.disk_io) == (None, None)
    assert profile.pool_path.endswith("Library/Application Support/mini-vps/vps-pool")


def test_detect_macos_intel_uses_q35():
    profile = detect(system="Darwin", machine="x86_64", env={})
    assert (profile.arch, profile.machine, profile.domain_type) == (
        "x86_64",
        "q35",
        "hvf",
    )


def test_detect_env_overrides():
    env = {
        "MINIVPS_LIBVIRT_URI": "qemu+ssh://host/system",
        "MINIVPS_DATA_DIR": "/data",
        "MINIVPS_LOCK_DIR": "/tmp/locks",
        "MINIVPS_ACCEL": "tcg",
        "MINIVPS_DISK_CACHE": "",
        "MINIVPS_SSH_PORT_RANGE": "3000-3010",
    }
    profile = detect(system="Linux", machine="x86_64", env=env, kvm_available=True)
    assert profile.libvirt_uri == "qemu+ssh://host/system"
    assert profile.pool_path == "/data/vps-pool"
    assert profile.lock_dir == "/tmp/locks"
    assert profile.domain_type == "qemu"
    # 空文字は libvirt の既定(属性を出さない)を意味する
    assert profile.disk_cache is None
    assert profile.ssh_port_range == (3000, 3010)


@pytest.mark.parametrize("bad", ["3000", "10-5", "0-10"])
def test_detect_rejects_invalid_port_range(bad):
    with pytest.raises(ValueError):
        detect(system="Linux", machine="x86_64", env={"MINIVPS_SSH_PORT_RANGE": bad})


def test_set_profile_overrides_and_resets():
    custom = detect(system="Darwin", machine="arm64", env={})
    set_profile(custom)
    assert get_profile() is custom
    set_profile(None)
    assert get_profile() is not custom
