import pytest
import yaml
from pydantic import ValidationError

from mini_vps.spec import (
    SAMPLE_SPEC,
    FilterRule,
    NetworkAttachment,
    ServerSpec,
    StaticRoute,
    load_spec,
)


def _base_spec_dict(**overrides):
    spec = {
        "name": "web-1",
        "memory": 1024,
        "vcpus": 2,
        "base_image": "ubuntu-24.04.img",
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


# --- StaticRoute ---


def test_static_route_accepts_valid_destination_and_via():
    route = StaticRoute(destination="192.168.202.0/24", via="192.168.201.1")
    assert str(route.destination) == "192.168.202.0/24"
    assert str(route.via) == "192.168.201.1"


def test_static_route_rejects_destination_with_host_bits_set():
    with pytest.raises(ValidationError):
        StaticRoute(destination="192.168.202.5/24", via="192.168.201.1")


@pytest.mark.parametrize("value", ["not-a-network", "192.168.202.0/33", ""])
def test_static_route_rejects_invalid_destination(value):
    with pytest.raises(ValidationError):
        StaticRoute(destination=value, via="192.168.201.1")


@pytest.mark.parametrize("value", ["not-an-ip", "192.168.201.0/24", ""])
def test_static_route_rejects_invalid_via(value):
    with pytest.raises(ValidationError):
        StaticRoute(destination="192.168.202.0/24", via=value)


def test_static_route_serializes_ips_as_strings():
    route = StaticRoute(destination="192.168.202.0/24", via="192.168.201.1")
    dumped = route.model_dump()
    assert dumped == {"destination": "192.168.202.0/24", "via": "192.168.201.1"}


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
    assert spec.networks == ["default"]
    assert spec.filters is None
    assert spec.static_routes == []


def test_server_spec_requires_name():
    payload = _base_spec_dict()
    del payload["name"]
    with pytest.raises(ValidationError):
        ServerSpec(**payload)


# --- name/network/hostname の文字種制約 ---


@pytest.mark.parametrize("value", ["web-1", "web_1", "a", "A9", "x" * 63])
def test_server_spec_accepts_valid_name_pattern(value):
    spec = ServerSpec(**_base_spec_dict(name=value))
    assert spec.name == value


@pytest.mark.parametrize(
    "value",
    ["", "a/b", "a'b", "<x>", "a b", "-leading-hyphen", "x" * 64],
)
def test_server_spec_rejects_invalid_name_pattern(value):
    with pytest.raises(ValidationError):
        ServerSpec(**_base_spec_dict(name=value))


@pytest.mark.parametrize("value", ["default", "vps-net", "net_1"])
def test_server_spec_accepts_valid_network_pattern(value):
    spec = ServerSpec(**_base_spec_dict(networks=[value]))
    assert spec.networks == [value]


@pytest.mark.parametrize("value", ["a/b", "a'b", "<x>", "default;evil"])
def test_server_spec_rejects_invalid_network_pattern(value):
    with pytest.raises(ValidationError):
        ServerSpec(**_base_spec_dict(networks=[value]))


def test_server_spec_accepts_multiple_networks():
    spec = ServerSpec(**_base_spec_dict(networks=["seg1", "seg2"]))
    assert spec.networks == ["seg1", "seg2"]


def test_server_spec_rejects_empty_networks():
    with pytest.raises(ValidationError):
        ServerSpec(**_base_spec_dict(networks=[]))


def test_server_spec_rejects_duplicate_networks():
    with pytest.raises(ValidationError):
        ServerSpec(**_base_spec_dict(networks=["seg1", "seg1"]))


# --- NetworkAttachment(静的IP) ---


def test_network_attachment_accepts_valid_address_and_gateway():
    attachment = NetworkAttachment(
        name="seg1", address="192.168.201.10/24", gateway="192.168.201.1"
    )
    assert str(attachment.address) == "192.168.201.10/24"
    assert str(attachment.gateway) == "192.168.201.1"


def test_network_attachment_gateway_defaults_to_none():
    attachment = NetworkAttachment(name="seg1", address="192.168.201.10/24")
    assert attachment.gateway is None


@pytest.mark.parametrize("value", ["not-an-address", "192.168.201.10/33", ""])
def test_network_attachment_rejects_invalid_address(value):
    with pytest.raises(ValidationError):
        NetworkAttachment(name="seg1", address=value)


def test_network_attachment_rejects_invalid_gateway():
    with pytest.raises(ValidationError):
        NetworkAttachment(name="seg1", address="192.168.201.10/24", gateway="not-an-ip")


def test_network_attachment_serializes_ips_as_strings():
    attachment = NetworkAttachment(
        name="seg1", address="192.168.201.10/24", gateway="192.168.201.1"
    )
    dumped = attachment.model_dump()
    assert dumped == {
        "name": "seg1",
        "address": "192.168.201.10/24",
        "gateway": "192.168.201.1",
    }
    yaml.safe_dump(dumped)


def test_network_attachment_serializes_absent_gateway_as_none():
    attachment = NetworkAttachment(name="seg1", address="192.168.201.10/24")
    dumped = attachment.model_dump()
    assert dumped["gateway"] is None


def test_server_spec_accepts_mixed_dhcp_and_static_networks():
    spec = ServerSpec(
        **_base_spec_dict(
            networks=["default", {"name": "seg1", "address": "192.168.201.10/24"}]
        )
    )
    assert spec.networks[0] == "default"
    assert spec.networks[1] == NetworkAttachment(
        name="seg1", address="192.168.201.10/24"
    )


def test_server_spec_rejects_duplicate_networks_between_string_and_attachment():
    with pytest.raises(ValidationError):
        ServerSpec(
            **_base_spec_dict(
                networks=["seg1", {"name": "seg1", "address": "192.168.201.10/24"}]
            )
        )


def test_server_spec_dump_serializes_static_network_as_string_values():
    spec = ServerSpec(
        **_base_spec_dict(networks=[{"name": "seg1", "address": "192.168.201.10/24"}])
    )
    dumped = spec.model_dump()
    assert dumped["networks"] == [
        {"name": "seg1", "address": "192.168.201.10/24", "gateway": None}
    ]
    yaml.safe_dump(dumped)


# --- static_routes ---


def test_server_spec_accepts_static_routes():
    spec = ServerSpec(
        **_base_spec_dict(
            static_routes=[{"destination": "192.168.202.0/24", "via": "192.168.201.1"}]
        )
    )
    assert spec.static_routes == [
        StaticRoute(destination="192.168.202.0/24", via="192.168.201.1")
    ]


def test_server_spec_dump_serializes_static_routes_as_strings():
    spec = ServerSpec(
        **_base_spec_dict(
            static_routes=[{"destination": "192.168.202.0/24", "via": "192.168.201.1"}]
        )
    )
    dumped = spec.model_dump()
    assert dumped["static_routes"] == [
        {"destination": "192.168.202.0/24", "via": "192.168.201.1"}
    ]
    # yaml.safe_dump できること(libvirt metadata への永続化と同じ経路)を確認する
    yaml.safe_dump(dumped)


@pytest.mark.parametrize("value", ["a/b", "a'b", "<x>", "a b"])
def test_server_spec_rejects_invalid_hostname_pattern(value):
    with pytest.raises(ValidationError):
        ServerSpec(**_base_spec_dict(hostname=value))


# --- user の文字種制約 ---


@pytest.mark.parametrize("value", ["ubuntu", "_svc", "web-1", "a" * 32])
def test_server_spec_accepts_valid_user_pattern(value):
    spec = ServerSpec(**_base_spec_dict(user=value))
    assert spec.user == value


@pytest.mark.parametrize(
    "value", ["Ubuntu", "root;rm -rf /", "user name", "9user", "a" * 33]
)
def test_server_spec_rejects_invalid_user_pattern(value):
    with pytest.raises(ValidationError):
        ServerSpec(**_base_spec_dict(user=value))


# --- memory/vcpus/disk の正数制約 ---


@pytest.mark.parametrize("field", ["memory", "vcpus", "disk"])
@pytest.mark.parametrize("value", [0, -1])
def test_server_spec_rejects_non_positive_numeric_fields(field, value):
    with pytest.raises(ValidationError):
        ServerSpec(**_base_spec_dict(**{field: value}))


# --- startup_script ---


def test_server_spec_startup_script_defaults_to_none():
    spec = ServerSpec(**_base_spec_dict())
    assert spec.startup_script is None


def test_server_spec_accepts_known_startup_script():
    spec = ServerSpec(**_base_spec_dict(startup_script="opencode-sakura-ai-engine"))
    assert spec.startup_script == "opencode-sakura-ai-engine"


def test_server_spec_rejects_unknown_startup_script():
    with pytest.raises(ValidationError):
        ServerSpec(**_base_spec_dict(startup_script="no-such-template"))


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
