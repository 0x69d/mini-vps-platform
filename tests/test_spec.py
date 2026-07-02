import pytest
import yaml
from pydantic import ValidationError

from mini_vps.spec import SAMPLE_SPEC, FilterRule, ServerSpec, load_spec


def _base_spec_dict(**overrides):
    spec = {
        "name": "web-1",
        "memory": 1024,
        "vcpus": 2,
        "base_image": "ubuntu-noble.img",
        "disk": 10,
    }
    spec.update(overrides)
    return spec


# --- FilterRule ---


@pytest.mark.parametrize("protocol", ["tcp", "udp"])
@pytest.mark.parametrize("port", [1, 22, 65535])
def test_filter_rule_accepts_valid(protocol, port):
    rule = FilterRule(port=port, protocol=protocol)
    assert rule.port == port
    assert rule.protocol == protocol


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_filter_rule_rejects_out_of_range_port(port):
    with pytest.raises(ValidationError):
        FilterRule(port=port, protocol="tcp")


def test_filter_rule_rejects_unknown_protocol():
    with pytest.raises(ValidationError):
        FilterRule(port=80, protocol="icmp")


# --- ServerSpec ---


def test_server_spec_defaults_hostname_from_name():
    spec = ServerSpec(**_base_spec_dict())
    assert spec.hostname == "web-1"


def test_server_spec_keeps_explicit_hostname():
    spec = ServerSpec(**_base_spec_dict(hostname="custom"))
    assert spec.hostname == "custom"


def test_server_spec_applies_field_defaults():
    spec = ServerSpec(**_base_spec_dict())
    assert spec.user == "ubuntu"
    assert spec.network == "default"
    assert spec.filters is None


def test_server_spec_requires_name():
    payload = _base_spec_dict()
    del payload["name"]
    with pytest.raises(ValidationError):
        ServerSpec(**payload)


# --- load_spec ---


def test_load_spec_returns_normalized_dict():
    text = yaml.safe_dump(_base_spec_dict(hostname=None))
    result = load_spec(text)
    assert result["name"] == "web-1"
    # hostname は name で補完される
    assert result["hostname"] == "web-1"
    assert result["user"] == "ubuntu"


def test_load_spec_parses_sample_spec():
    result = load_spec(SAMPLE_SPEC)
    assert result["name"] == "web-1"
    assert result["memory"] == 1024
    assert result["vcpus"] == 2


def test_load_spec_rejects_missing_required_key():
    text = yaml.safe_dump({"name": "web-1"})
    with pytest.raises(ValidationError):
        load_spec(text)
