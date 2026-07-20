import subprocess

import pytest

from mini_vps import dns_registration

_ENV_VARS = (
    "MINIVPS_DNS_SERVER",
    "MINIVPS_DNS_ZONE",
    "MINIVPS_DNS_TSIG_KEY_FILE",
)

_ENV_VALUES = {
    "MINIVPS_DNS_SERVER": "192.168.203.30",
    "MINIVPS_DNS_ZONE": "minivps.internal",
    "MINIVPS_DNS_TSIG_KEY_FILE": "/home/user/.config/minivps/dns-tsig.key",
}


def _spec(**overrides):
    spec = {
        "name": "web-1",
        "networks": [
            {
                "name": "seg1",
                "address": "192.168.201.50/24",
                "gateway": "192.168.201.1",
                "nameservers": [],
                "search": [],
            }
        ],
    }
    spec.update(overrides)
    return spec


def _set_env(monkeypatch, *, omit=()):
    for var in _ENV_VARS:
        if var in omit:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, _ENV_VALUES[var])


def _capture_run(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("mini_vps.dns_registration.subprocess.run", fake_run)
    return captured


# --- 有効化条件(3環境変数がすべて設定されているときのみ有効) ---


@pytest.mark.parametrize(
    "omit",
    [
        ("MINIVPS_DNS_SERVER",),
        ("MINIVPS_DNS_ZONE",),
        ("MINIVPS_DNS_TSIG_KEY_FILE",),
        ("MINIVPS_DNS_SERVER", "MINIVPS_DNS_ZONE"),
        ("MINIVPS_DNS_SERVER", "MINIVPS_DNS_TSIG_KEY_FILE"),
        ("MINIVPS_DNS_ZONE", "MINIVPS_DNS_TSIG_KEY_FILE"),
        _ENV_VARS,
    ],
)
@pytest.mark.parametrize("func", ["register", "unregister"])
def test_disabled_when_any_env_var_missing(monkeypatch, omit, func):
    """1つでも環境変数が欠ければ nsupdate は一切呼ばれない(完全無効)。"""
    _set_env(monkeypatch, omit=omit)
    captured = _capture_run(monkeypatch)

    getattr(dns_registration, func)(_spec())

    assert captured == {}


@pytest.mark.parametrize("func", ["register", "unregister"])
def test_enabled_when_all_env_vars_present(monkeypatch, func):
    _set_env(monkeypatch)
    captured = _capture_run(monkeypatch)

    getattr(dns_registration, func)(_spec())

    assert captured["cmd"][0] == "nsupdate"


# --- nsupdate の呼び出し内容 ---


def test_register_builds_idempotent_a_and_ptr_updates(monkeypatch):
    """A と PTR を update delete→add の冪等な組・send 2分割で登録する。"""
    _set_env(monkeypatch)
    captured = _capture_run(monkeypatch)

    dns_registration.register(_spec())

    assert captured["cmd"] == [
        "nsupdate",
        "-k",
        "/home/user/.config/minivps/dns-tsig.key",
        "-t",
        "5",
    ]
    # A(minivps.internal)と PTR(in-addr.arpa)は別ゾーンのため、
    # RFC 2136(1メッセージ=1ゾーン)に従い send を2回に分ける。
    assert captured["kwargs"]["input"] == (
        "server 192.168.203.30\n"
        "update delete web-1.minivps.internal. A\n"
        "update add web-1.minivps.internal. 300 A 192.168.201.50\n"
        "send\n"
        "update delete 50.201.168.192.in-addr.arpa. PTR\n"
        "update add 50.201.168.192.in-addr.arpa. 300 PTR web-1.minivps.internal.\n"
        "send\n"
    )
    assert captured["kwargs"]["timeout"] == 15
    assert captured["kwargs"]["check"] is True


def test_unregister_builds_delete_only_updates(monkeypatch):
    _set_env(monkeypatch)
    captured = _capture_run(monkeypatch)

    dns_registration.unregister(_spec())

    assert captured["kwargs"]["input"] == (
        "server 192.168.203.30\n"
        "update delete web-1.minivps.internal. A\n"
        "send\n"
        "update delete 50.201.168.192.in-addr.arpa. PTR\n"
        "send\n"
    )


def test_register_normalizes_zone_trailing_dot(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("MINIVPS_DNS_ZONE", "minivps.internal.")
    captured = _capture_run(monkeypatch)

    dns_registration.register(_spec())

    assert "update add web-1.minivps.internal. 300 A" in captured["kwargs"]["input"]


def test_register_uses_first_static_nic_among_multiple(monkeypatch):
    """DHCP 要素を飛ばして最初の静的NICの IP を使う(_static_ipv4 と同規約)。"""
    _set_env(monkeypatch)
    captured = _capture_run(monkeypatch)

    dns_registration.register(
        _spec(
            networks=[
                "default",
                {"name": "seg1", "address": "192.168.201.60/24", "gateway": None},
                {"name": "seg2", "address": "192.168.202.60/24", "gateway": None},
            ]
        )
    )

    assert "300 A 192.168.201.60" in captured["kwargs"]["input"]


# --- 静的NICなしのスキップ ---


@pytest.mark.parametrize("func", ["register", "unregister"])
def test_skips_vm_without_static_nic(monkeypatch, caplog, func):
    _set_env(monkeypatch)
    captured = _capture_run(monkeypatch)

    with caplog.at_level("INFO", logger="mini_vps.dns_registration"):
        getattr(dns_registration, func)(_spec(networks=["default"]))

    assert captured == {}
    assert any("no static NIC" in r.message for r in caplog.records)


# --- ベストエフォート(例外を呼び出し元に漏らさない) ---


@pytest.mark.parametrize(
    "side_effect",
    [
        subprocess.CalledProcessError(2, "nsupdate", stderr="update failed: SERVFAIL"),
        subprocess.TimeoutExpired("nsupdate", 15),
        FileNotFoundError("nsupdate"),
    ],
)
@pytest.mark.parametrize("func", ["register", "unregister"])
def test_failures_are_logged_not_raised(monkeypatch, caplog, side_effect, func):
    _set_env(monkeypatch)

    def fake_run(cmd, **kwargs):
        raise side_effect

    monkeypatch.setattr("mini_vps.dns_registration.subprocess.run", fake_run)

    with caplog.at_level("WARNING", logger="mini_vps.dns_registration"):
        getattr(dns_registration, func)(_spec())  # 例外が漏れれば失敗する

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "dns" in warnings[0].message
