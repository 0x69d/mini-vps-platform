from mini_vps.resources import (
    _filter_name,
    build_domain_xml,
    build_nwfilter_xml,
)


def _spec(**overrides):
    spec = {
        "name": "web-1",
        "hostname": "web-1",
        "user": "ubuntu",
        "memory": 1024,
        "vcpus": 2,
        "base_image": "ubuntu-noble.img",
        "disk": 10,
        "network": "default",
    }
    spec.update(overrides)
    return spec


# --- _filter_name ---


def test_filter_name_is_deterministic():
    assert _filter_name({"name": "web-1"}) == "minivps-web-1"


# --- build_nwfilter_xml ---


def test_build_nwfilter_xml_expands_each_rule():
    spec = _spec(
        filters=[{"port": 22, "protocol": "tcp"}, {"port": 53, "protocol": "udp"}]
    )
    xml = build_nwfilter_xml(spec)

    assert "filter name='minivps-web-1'" in xml
    assert "<tcp dstportstart='22'/>" in xml
    assert "<udp dstportstart='53'/>" in xml
    # ステートフルな戻り通信の許可と default drop が常に入る
    assert "ESTABLISHED,RELATED" in xml
    assert "action='drop'" in xml


def test_build_nwfilter_xml_with_empty_filters_has_no_port_rules():
    xml = build_nwfilter_xml(_spec(filters=[]))
    assert "dstportstart" not in xml
    # 空でも filter 骨格と default drop は成立する
    assert "filter name='minivps-web-1'" in xml
    assert "action='drop'" in xml


# --- build_domain_xml ---


def test_build_domain_xml_converts_memory_to_kib():
    xml = build_domain_xml(_spec(), "/overlay.qcow2", "/seed.iso")
    assert "<memory unit='KiB'>1048576</memory>" in xml


def test_build_domain_xml_embeds_paths_and_fields():
    xml = build_domain_xml(_spec(), "/lab/web-1.qcow2", "/lab/web-1-seed.iso")
    assert "<name>web-1</name>" in xml
    assert "<vcpu>2</vcpu>" in xml
    assert "source file='/lab/web-1.qcow2'" in xml
    assert "source file='/lab/web-1-seed.iso'" in xml
    assert "source network='default'" in xml


def test_build_domain_xml_without_filter_omits_filterref():
    xml = build_domain_xml(_spec(), "/overlay.qcow2", "/seed.iso")
    assert "filterref" not in xml


def test_build_domain_xml_with_filter_adds_filterref():
    xml = build_domain_xml(
        _spec(), "/overlay.qcow2", "/seed.iso", filter_name="minivps-web-1"
    )
    assert "<filterref filter='minivps-web-1'/>" in xml


def test_build_domain_xml_defaults_network_when_absent():
    spec = _spec()
    del spec["network"]
    xml = build_domain_xml(spec, "/overlay.qcow2", "/seed.iso")
    assert "source network='default'" in xml
