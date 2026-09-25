import pathlib

from conftest import macos_profile

from mini_vps.planning import check_platform
from mini_vps.resources import build_domain_xml
from mini_vps.spec import load_spec

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"


def test_macos_quickstart_is_valid_on_macos():
    # docs/macos.md の手順どおりに作れること(macOS で拒否される機能を含まない)。
    spec = load_spec((EXAMPLES / "macos-quickstart.yaml").read_text())
    profile = macos_profile()
    check_platform(spec, profile)
    build_domain_xml(spec, "/o.qcow2", "/s.iso", profile=profile, ssh_port=2201)
    assert spec["base_image"] == "ubuntu-24.04.img"
