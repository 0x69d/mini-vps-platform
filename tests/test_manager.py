from unittest.mock import MagicMock

import libvirt
import pytest
from conftest import make_libvirt_error

from mini_vps.manager import (
    ServerConflict,
    ServerManager,
    ServerNotFound,
    ServerNotRunning,
    ServerRunning,
    _find_domain,
    _is_managed,
    _lookup,
    _read_spec,
    _status_of,
    _write_spec,
    register_quiet_error_handler,
)

# --- register_quiet_error_handler ---


def test_register_quiet_error_handler_registers_noop_handler(monkeypatch):
    captured = {}

    def _register(handler, ctx):
        captured["handler"] = handler
        captured["ctx"] = ctx

    monkeypatch.setattr(libvirt, "registerErrorHandler", _register)

    register_quiet_error_handler()

    assert captured["ctx"] is None
    # 登録したハンドラ自体は何もしない(例外を投げない)ことだけを確認する。
    captured["handler"](None, None)


# --- _write_spec / _read_spec ---


def test_write_then_read_spec_roundtrips():
    dom = MagicMock()
    captured = {}

    def _set_metadata(kind, xml, key, ns, flags):
        captured["xml"] = xml

    dom.setMetadata.side_effect = _set_metadata
    dom.metadata.side_effect = lambda *a, **k: captured["xml"]

    spec = {"name": "web-1", "memory": 1024}
    _write_spec(dom, spec)

    assert _read_spec(dom) == spec


# --- _lookup ---


def test_lookup_returns_domain_when_managed():
    conn = MagicMock()
    dom = MagicMock()
    conn.lookupByName.return_value = dom

    assert _lookup(conn, "web-1") is dom


def test_lookup_raises_not_found_when_domain_missing():
    conn = MagicMock()
    conn.lookupByName.side_effect = make_libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)

    with pytest.raises(ServerNotFound):
        _lookup(conn, "web-1")


def test_lookup_raises_not_found_when_metadata_missing():
    conn = MagicMock()
    dom = MagicMock()
    conn.lookupByName.return_value = dom
    dom.metadata.side_effect = make_libvirt_error(libvirt.VIR_ERR_NO_DOMAIN_METADATA)

    with pytest.raises(ServerNotFound):
        _lookup(conn, "web-1")


def test_lookup_reraises_other_libvirt_errors():
    conn = MagicMock()
    conn.lookupByName.side_effect = make_libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)

    with pytest.raises(libvirt.libvirtError):
        _lookup(conn, "web-1")


# --- _find_domain ---


def test_find_domain_returns_domain_when_present():
    conn = MagicMock()
    dom = MagicMock()
    conn.lookupByName.return_value = dom

    assert _find_domain(conn, "web-1") is dom


def test_find_domain_returns_none_when_missing():
    conn = MagicMock()
    conn.lookupByName.side_effect = make_libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)

    assert _find_domain(conn, "web-1") is None


def test_find_domain_reraises_other_errors():
    conn = MagicMock()
    conn.lookupByName.side_effect = make_libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)

    with pytest.raises(libvirt.libvirtError):
        _find_domain(conn, "web-1")


# --- _status_of ---


def test_status_of_running_includes_ip(monkeypatch):
    dom = MagicMock()
    dom.state.return_value = (libvirt.VIR_DOMAIN_RUNNING, 1)
    lease = MagicMock(return_value="10.0.0.5")
    monkeypatch.setattr("mini_vps.manager._lease_ipv4", lease)

    assert _status_of(dom) == {"state": "running", "ip": "10.0.0.5"}
    lease.assert_called_once_with(dom)


def test_status_of_non_running_has_no_ip(monkeypatch):
    dom = MagicMock()
    dom.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 1)
    lease = MagicMock()
    monkeypatch.setattr("mini_vps.manager._lease_ipv4", lease)

    assert _status_of(dom) == {"state": "shutoff", "ip": None}
    # 起動中でなければリースは引かない
    lease.assert_not_called()


# --- ServerManager.create ---


def _full_spec(**overrides):
    spec = {
        "name": "web-1",
        "memory": 1024,
        "vcpus": 2,
        "base_image": "ubuntu-24.04.img",
        "disk": 10,
    }
    spec.update(overrides)
    return spec


def test_create_is_idempotent_when_spec_matches(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    spec = {"name": "web-1"}
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: MagicMock())
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda dom: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda dom: spec)
    provision_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.provision", provision_mock)
    mgr.get = MagicMock(return_value={"spec": spec, "status": {}})

    result, created = mgr.create(spec)

    assert created is False
    provision_mock.assert_not_called()
    assert result == mgr.get.return_value


def test_create_raises_conflict_when_spec_differs(monkeypatch):
    """不変フィールド(disk)の差分は収束対象外のため ServerConflict になる。"""
    conn = MagicMock()
    mgr = ServerManager(conn)
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: MagicMock())
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda dom: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda dom: _full_spec())

    with pytest.raises(ServerConflict):
        mgr.create(_full_spec(disk=20))


def test_create_raises_conflict_when_existing_is_unmanaged(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: MagicMock())
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda dom: False)

    with pytest.raises(ServerConflict):
        mgr.create(_full_spec())


def test_create_provisions_new_when_absent(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: None)
    monkeypatch.setattr("mini_vps.manager.provision", MagicMock(return_value=dom))
    write_spec_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager._write_spec", write_spec_mock)
    mgr.get = MagicMock(return_value={"spec": {"name": "web-1"}, "status": {}})

    result, created = mgr.create({"name": "web-1"})

    assert created is True
    write_spec_mock.assert_called_once_with(dom, {"name": "web-1"})
    dom.create.assert_called_once()


def test_create_rolls_back_on_failure(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: None)
    monkeypatch.setattr(
        "mini_vps.manager.provision", MagicMock(side_effect=RuntimeError("boom"))
    )
    teardown_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.teardown", teardown_mock)

    with pytest.raises(RuntimeError):
        mgr.create({"name": "web-1"})

    teardown_mock.assert_called_once_with(conn, {"name": "web-1"})


def test_create_forwards_secrets_to_provision(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    secrets = {"AI_ENGINE_TOKEN": "sk-abc"}
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: None)
    provision_mock = MagicMock(return_value=dom)
    monkeypatch.setattr("mini_vps.manager.provision", provision_mock)
    monkeypatch.setattr("mini_vps.manager._write_spec", MagicMock())
    mgr.get = MagicMock(return_value={"spec": {"name": "web-1"}, "status": {}})

    mgr.create({"name": "web-1"}, secrets=secrets)

    provision_mock.assert_called_once_with(conn, {"name": "web-1"}, secrets=secrets)


def test_create_never_writes_secrets_into_spec_metadata(monkeypatch):
    """secrets が _write_spec(→ libvirt metadata)に渡らないことを保証する回帰テスト。"""
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    spec = {"name": "web-1"}
    secrets = {"AI_ENGINE_TOKEN": "sk-abc"}
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: None)
    monkeypatch.setattr("mini_vps.manager.provision", MagicMock(return_value=dom))
    write_spec_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager._write_spec", write_spec_mock)
    mgr.get = MagicMock(return_value={"spec": spec, "status": {}})

    mgr.create(spec, secrets=secrets)

    # _write_spec に渡る spec は secrets を一切含まない(そのままの spec dict)
    write_spec_mock.assert_called_once_with(dom, spec)
    assert "secrets" not in write_spec_mock.call_args[0][1]


# --- ServerManager.create (可変フィールドの収束) ---


def test_create_raises_server_running_when_mutable_diff_and_active(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = True
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda d: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: _full_spec())

    with pytest.raises(ServerRunning):
        mgr.create(_full_spec(memory=2048))

    dom.XMLDesc.assert_not_called()
    conn.defineXML.assert_not_called()


def test_create_converges_memory_only_when_stopped(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    dom.XMLDesc.return_value = "<domain/>"
    old_spec = _full_spec()
    new_spec = _full_spec(memory=2048)
    new_dom = MagicMock()
    conn.defineXML.return_value = new_dom
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda d: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: old_spec)
    write_spec_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager._write_spec", write_spec_mock)
    resize_xml_mock = MagicMock(return_value="<domain resized/>")
    monkeypatch.setattr("mini_vps.manager.resize_domain_xml", resize_xml_mock)
    mgr.get = MagicMock(return_value={"spec": new_spec, "status": {}})

    result, created = mgr.create(new_spec)

    assert created is False
    dom.XMLDesc.assert_called_once_with(libvirt.VIR_DOMAIN_XML_INACTIVE)
    resize_xml_mock.assert_called_once_with("<domain/>", 2048 * 1024, 2)
    conn.defineXML.assert_called_once_with("<domain resized/>")
    write_spec_mock.assert_called_once_with(new_dom, new_spec)
    assert result == mgr.get.return_value


def test_create_converges_vcpus_only_when_stopped(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    dom.XMLDesc.return_value = "<domain/>"
    old_spec = _full_spec()
    new_spec = _full_spec(vcpus=4)
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda d: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: old_spec)
    monkeypatch.setattr("mini_vps.manager._write_spec", MagicMock())
    resize_xml_mock = MagicMock(return_value="<domain resized/>")
    monkeypatch.setattr("mini_vps.manager.resize_domain_xml", resize_xml_mock)

    mgr.create(new_spec)

    resize_xml_mock.assert_called_once_with("<domain/>", 1024 * 1024, 4)


def test_create_converges_memory_and_vcpus_together(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    dom.XMLDesc.return_value = "<domain/>"
    old_spec = _full_spec()
    new_spec = _full_spec(memory=4096, vcpus=8)
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda d: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: old_spec)
    monkeypatch.setattr("mini_vps.manager._write_spec", MagicMock())
    resize_xml_mock = MagicMock(return_value="<domain resized/>")
    monkeypatch.setattr("mini_vps.manager.resize_domain_xml", resize_xml_mock)

    mgr.create(new_spec)

    resize_xml_mock.assert_called_once_with("<domain/>", 4096 * 1024, 8)


def test_create_converges_filters_none_to_list_defines_and_attaches_filter(
    monkeypatch,
):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    dom.XMLDesc.return_value = "<domain/>"
    old_spec = _full_spec(filters=None)
    new_spec = _full_spec(filters=[{"port": 22, "protocol": "tcp"}])
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda d: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: old_spec)
    monkeypatch.setattr("mini_vps.manager._write_spec", MagicMock())
    nwfilter_xml_mock = MagicMock(return_value="<filter/>")
    monkeypatch.setattr("mini_vps.manager.build_nwfilter_xml", nwfilter_xml_mock)
    set_filterref_mock = MagicMock(return_value="<domain filtered/>")
    monkeypatch.setattr("mini_vps.manager.set_domain_filterref_xml", set_filterref_mock)

    mgr.create(new_spec)

    nwfilter_xml_mock.assert_called_once_with(new_spec)
    conn.nwfilterDefineXML.assert_called_once_with("<filter/>")
    set_filterref_mock.assert_called_once_with("<domain/>", "minivps-web-1")
    conn.defineXML.assert_called_once_with("<domain filtered/>")
    conn.nwfilterLookupByName.assert_not_called()


def test_create_converges_filters_none_to_empty_list_defines_deny_all_filter(
    monkeypatch,
):
    """filters=[] は「フィルタ無し」ではなく「全 inbound 拒否」を意味する。

    is not None ではなく truthy 判定(if filters:)への退行があると、この
    ケースで nwfilterDefineXML/filterref 付与が呼ばれなくなり検知できる。
    """
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    dom.XMLDesc.return_value = "<domain/>"
    old_spec = _full_spec(filters=None)
    new_spec = _full_spec(filters=[])
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda d: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: old_spec)
    monkeypatch.setattr("mini_vps.manager._write_spec", MagicMock())
    nwfilter_xml_mock = MagicMock(return_value="<filter/>")
    monkeypatch.setattr("mini_vps.manager.build_nwfilter_xml", nwfilter_xml_mock)
    set_filterref_mock = MagicMock(return_value="<domain filtered/>")
    monkeypatch.setattr("mini_vps.manager.set_domain_filterref_xml", set_filterref_mock)

    mgr.create(new_spec)

    nwfilter_xml_mock.assert_called_once_with(new_spec)
    conn.nwfilterDefineXML.assert_called_once_with("<filter/>")
    set_filterref_mock.assert_called_once_with("<domain/>", "minivps-web-1")


def test_create_converges_filters_list_to_none_detaches_before_undefining(
    monkeypatch,
):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    dom.XMLDesc.return_value = "<domain filtered/>"
    old_spec = _full_spec(filters=[{"port": 22, "protocol": "tcp"}])
    new_spec = _full_spec(filters=None)
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda d: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: old_spec)
    monkeypatch.setattr("mini_vps.manager._write_spec", MagicMock())
    set_filterref_mock = MagicMock(return_value="<domain unfiltered/>")
    monkeypatch.setattr("mini_vps.manager.set_domain_filterref_xml", set_filterref_mock)

    mgr.create(new_spec)

    set_filterref_mock.assert_called_once_with("<domain filtered/>", None)
    conn.nwfilterDefineXML.assert_not_called()
    conn.nwfilterLookupByName.assert_called_once_with("minivps-web-1")
    # nwfilter は filterref から参照されている間 undefine できないため、
    # defineXML(filterref 除去)が undefine より先に呼ばれている必要がある。
    call_names = [c[0] for c in conn.mock_calls]
    assert call_names.index("defineXML") < call_names.index(
        "nwfilterLookupByName().undefine"
    )


def test_create_converges_filters_list_to_list_redefines_without_undefining(
    monkeypatch,
):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    dom.XMLDesc.return_value = "<domain filtered/>"
    old_spec = _full_spec(filters=[{"port": 22, "protocol": "tcp"}])
    new_spec = _full_spec(filters=[{"port": 80, "protocol": "tcp"}])
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda d: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: old_spec)
    monkeypatch.setattr("mini_vps.manager._write_spec", MagicMock())
    nwfilter_xml_mock = MagicMock(return_value="<filter/>")
    monkeypatch.setattr("mini_vps.manager.build_nwfilter_xml", nwfilter_xml_mock)
    monkeypatch.setattr(
        "mini_vps.manager.set_domain_filterref_xml",
        MagicMock(return_value="<domain refiltered/>"),
    )

    mgr.create(new_spec)

    nwfilter_xml_mock.assert_called_once_with(new_spec)
    conn.nwfilterDefineXML.assert_called_once_with("<filter/>")
    conn.nwfilterLookupByName.assert_not_called()


def test_create_converges_filters_and_memory_together_in_single_definexml(
    monkeypatch,
):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    dom.XMLDesc.return_value = "<domain/>"
    old_spec = _full_spec(filters=None)
    new_spec = _full_spec(memory=2048, filters=[{"port": 22, "protocol": "tcp"}])
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda d: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: old_spec)
    monkeypatch.setattr("mini_vps.manager._write_spec", MagicMock())
    monkeypatch.setattr(
        "mini_vps.manager.build_nwfilter_xml", MagicMock(return_value="<filter/>")
    )
    monkeypatch.setattr(
        "mini_vps.manager.resize_domain_xml",
        MagicMock(return_value="<domain resized/>"),
    )
    monkeypatch.setattr(
        "mini_vps.manager.set_domain_filterref_xml",
        MagicMock(return_value="<domain resized filtered/>"),
    )

    mgr.create(new_spec)

    dom.XMLDesc.assert_called_once_with(libvirt.VIR_DOMAIN_XML_INACTIVE)
    conn.defineXML.assert_called_once_with("<domain resized filtered/>")


# --- ServerManager.list ---


def test_list_filters_unmanaged_domains():
    conn = MagicMock()
    managed = MagicMock()
    managed.name.return_value = "web-1"
    unmanaged = MagicMock()
    unmanaged.metadata.side_effect = make_libvirt_error(
        libvirt.VIR_ERR_NO_DOMAIN_METADATA
    )
    conn.listAllDomains.return_value = [managed, unmanaged]
    mgr = ServerManager(conn)

    assert mgr.list() == ["web-1"]


# --- _is_managed / ServerManager.is_managed ---


def test_is_managed_true_when_metadata_present():
    dom = MagicMock()

    assert _is_managed(dom) is True
    assert ServerManager(MagicMock()).is_managed(dom) is True


def test_is_managed_false_when_metadata_missing():
    dom = MagicMock()
    dom.metadata.side_effect = make_libvirt_error(libvirt.VIR_ERR_NO_DOMAIN_METADATA)

    assert _is_managed(dom) is False
    assert ServerManager(MagicMock()).is_managed(dom) is False


# --- ServerManager.delete ---


def test_delete_tears_down_managed_server(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: MagicMock())
    teardown_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.teardown", teardown_mock)

    mgr.delete("web-1")

    teardown_mock.assert_called_once_with(conn, {"name": "web-1"})


def test_delete_raises_not_found_when_unmanaged(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)

    def _raise_not_found(c, n):
        raise ServerNotFound(n)

    monkeypatch.setattr("mini_vps.manager._lookup", _raise_not_found)
    teardown_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.teardown", teardown_mock)

    with pytest.raises(ServerNotFound):
        mgr.delete("web-1")

    teardown_mock.assert_not_called()


# --- ServerManager.start ---


def test_start_creates_inactive_domain(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    spec = {"name": "web-1"}
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: spec)
    ensure_network_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.ensure_network_active", ensure_network_mock)
    mgr.get = MagicMock(return_value={"spec": spec, "status": {}})

    mgr.start("web-1")

    ensure_network_mock.assert_called_once_with(conn, spec)
    dom.create.assert_called_once()


def test_start_is_noop_when_already_active(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = True
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
    ensure_network_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.ensure_network_active", ensure_network_mock)
    mgr.get = MagicMock(return_value={"spec": {}, "status": {}})

    mgr.start("web-1")

    ensure_network_mock.assert_not_called()
    dom.create.assert_not_called()


def test_start_raises_not_found_when_unmanaged(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)

    def _raise_not_found(c, n):
        raise ServerNotFound(n)

    monkeypatch.setattr("mini_vps.manager._lookup", _raise_not_found)

    with pytest.raises(ServerNotFound):
        mgr.start("web-1")


# --- ServerManager.stop ---


def test_stop_shuts_down_active_domain_by_default(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = True
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
    mgr.get = MagicMock(return_value={"spec": {}, "status": {}})

    mgr.stop("web-1")

    dom.shutdown.assert_called_once()
    dom.destroy.assert_not_called()


def test_stop_destroys_active_domain_when_forced(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = True
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
    mgr.get = MagicMock(return_value={"spec": {}, "status": {}})

    mgr.stop("web-1", force=True)

    dom.destroy.assert_called_once()
    dom.shutdown.assert_not_called()


def test_stop_is_noop_when_already_inactive(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
    mgr.get = MagicMock(return_value={"spec": {}, "status": {}})

    mgr.stop("web-1", force=True)

    dom.shutdown.assert_not_called()
    dom.destroy.assert_not_called()


def test_stop_raises_not_found_when_unmanaged(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)

    def _raise_not_found(c, n):
        raise ServerNotFound(n)

    monkeypatch.setattr("mini_vps.manager._lookup", _raise_not_found)

    with pytest.raises(ServerNotFound):
        mgr.stop("web-1")


# --- ServerManager.restart ---


def test_restart_reboots_by_default(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = True
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
    mgr.get = MagicMock(return_value={"spec": {}, "status": {}})

    mgr.restart("web-1")

    dom.reboot.assert_called_once()
    dom.destroy.assert_not_called()
    dom.create.assert_not_called()


def test_restart_raises_not_running_when_inactive_and_not_forced(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)

    with pytest.raises(ServerNotRunning):
        mgr.restart("web-1")

    dom.reboot.assert_not_called()


def test_restart_destroys_then_creates_active_domain_when_forced(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = True
    spec = {"name": "web-1"}
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: spec)
    ensure_network_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.ensure_network_active", ensure_network_mock)
    mgr.get = MagicMock(return_value={"spec": spec, "status": {}})

    mgr.restart("web-1", force=True)

    dom.destroy.assert_called_once()
    ensure_network_mock.assert_called_once_with(conn, spec)
    dom.create.assert_called_once()
    dom.reboot.assert_not_called()


def test_restart_skips_destroy_for_inactive_domain_when_forced(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    spec = {"name": "web-1"}
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: spec)
    ensure_network_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.ensure_network_active", ensure_network_mock)
    mgr.get = MagicMock(return_value={"spec": spec, "status": {}})

    mgr.restart("web-1", force=True)

    dom.destroy.assert_not_called()
    ensure_network_mock.assert_called_once_with(conn, spec)
    dom.create.assert_called_once()


def test_restart_raises_not_found_when_unmanaged(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)

    def _raise_not_found(c, n):
        raise ServerNotFound(n)

    monkeypatch.setattr("mini_vps.manager._lookup", _raise_not_found)

    with pytest.raises(ServerNotFound):
        mgr.restart("web-1")


# --- ServerManager.reinstall ---


def test_reinstall_destroys_active_domain_before_recreate(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = True
    spec = {"name": "web-1"}
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: spec)
    monkeypatch.setattr("mini_vps.manager.read_pubkey", lambda: "ssh-ed25519 AAAA")
    build_seed_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.build_seed_iso", build_seed_mock)
    create_overlay_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.create_overlay_volume", create_overlay_mock)
    ensure_network_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.ensure_network_active", ensure_network_mock)
    mgr.get = MagicMock(return_value={"spec": spec, "status": {}})

    mgr.reinstall("web-1")

    dom.destroy.assert_called_once()
    dom.create.assert_called_once()
    build_seed_mock.assert_called_once_with(spec, "ssh-ed25519 AAAA", secrets=None)
    create_overlay_mock.assert_called_once_with(conn, spec)
    ensure_network_mock.assert_called_once_with(conn, spec)


def test_reinstall_skips_destroy_when_inactive(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    spec = {"name": "web-1"}
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: spec)
    monkeypatch.setattr("mini_vps.manager.read_pubkey", lambda: "ssh-ed25519 AAAA")
    monkeypatch.setattr("mini_vps.manager.build_seed_iso", MagicMock())
    monkeypatch.setattr("mini_vps.manager.create_overlay_volume", MagicMock())
    monkeypatch.setattr("mini_vps.manager.ensure_network_active", MagicMock())
    mgr.get = MagicMock(return_value={"spec": spec, "status": {}})

    mgr.reinstall("web-1")

    dom.destroy.assert_not_called()
    dom.create.assert_called_once()


def test_reinstall_forwards_secrets_to_build_seed_iso(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    spec = {"name": "web-1"}
    secrets = {"AI_ENGINE_TOKEN": "sk-abc"}
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: spec)
    monkeypatch.setattr("mini_vps.manager.read_pubkey", lambda: "ssh-ed25519 AAAA")
    build_seed_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.build_seed_iso", build_seed_mock)
    monkeypatch.setattr("mini_vps.manager.create_overlay_volume", MagicMock())
    monkeypatch.setattr("mini_vps.manager.ensure_network_active", MagicMock())
    mgr.get = MagicMock(return_value={"spec": spec, "status": {}})

    mgr.reinstall("web-1", secrets=secrets)

    build_seed_mock.assert_called_once_with(spec, "ssh-ed25519 AAAA", secrets=secrets)
