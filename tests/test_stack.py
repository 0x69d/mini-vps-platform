import contextlib
import json
import logging
from unittest.mock import MagicMock, call

import pytest
from conftest import macos_profile
from fastapi.testclient import TestClient
from pydantic import ValidationError

import mini_vps.api as api_module
from mini_vps import cli
from mini_vps.errors import ServerConflict, ServerNotFound, StackError
from mini_vps.platform_profile import set_profile
from mini_vps.spec import ServerSpec
from mini_vps.stack import (
    StackDefinition,
    apply_stack,
    compute_plan,
    load_stack,
    plan_stack,
    resolve_stack,
    wait_until_ready,
)

STACK_YAML = """\
stack: agents
servers:
  - name: agent-1
    memory: 2048
    vcpus: 2
    base_image: ubuntu-24.04.img
    disk: 20
    depends_on: [dns-1]
  - name: dns-1
    memory: 1024
    vcpus: 1
    base_image: ubuntu-24.04.img
    disk: 10
  - name: agent-2
    memory: 2048
    vcpus: 2
    base_image: ubuntu-24.04.img
    disk: 20
    depends_on: [dns-1]
"""


def _server(name, **overrides):
    base = {
        "name": name,
        "memory": 1024,
        "vcpus": 1,
        "base_image": "ubuntu-24.04.img",
        "disk": 10,
    }
    base.update(overrides)
    return base


def _stack(*servers, name="agents"):
    return resolve_stack(StackDefinition(stack=name, servers=list(servers)))


def _observed(spec, state="running", ip="192.168.122.10"):
    """ServerManager.get() の結果(spec は ServerSpec で正規化)を作る。"""
    return {
        "spec": ServerSpec(**spec).model_dump(),
        "status": {"state": state, "ip": ip},
    }


def _fake_manager(existing=None):
    """既存 VM の dict を状態として持つ ServerManager の Mock を返す。

    create は VM を running・IP 付きで追加し、delete は取り除く。呼び出し順は
    mgr.mock_calls で検証する。
    """
    vms = dict(existing or {})
    mgr = MagicMock()
    mgr.list.side_effect = lambda: list(vms)

    def _get(name):
        if name not in vms:
            raise ServerNotFound(name)
        return vms[name]

    def _create(spec, secrets=None):
        created = spec["name"] not in vms
        vms[spec["name"]] = _observed(spec)
        return vms[spec["name"]], created

    def _delete(name):
        del vms[name]

    mgr.get.side_effect = _get
    mgr.status.side_effect = lambda name: _get(name)["status"]
    mgr.create.side_effect = _create
    mgr.delete.side_effect = _delete
    return mgr


def _write_calls(mgr):
    """create/delete の呼び出しだけを (操作, name) の列で返す。"""
    result = []
    for c in mgr.mock_calls:
        if c[0] == "create":
            result.append(("create", c.args[0]["name"]))
        elif c[0] == "delete":
            result.append(("delete", c.args[0]))
    return result


# --- spec の stack / depends_on ---


def test_spec_defaults_stack_and_depends_on():
    spec = ServerSpec(**_server("web-1")).model_dump()
    assert spec["stack"] is None
    assert spec["depends_on"] == []


def test_spec_rejects_depends_on_self():
    with pytest.raises(ValidationError, match="自分自身"):
        ServerSpec(**_server("web-1", depends_on=["web-1"]))


def test_spec_rejects_duplicate_depends_on():
    with pytest.raises(ValidationError, match="重複"):
        ServerSpec(**_server("web-1", depends_on=["dns-1", "dns-1"]))


def test_spec_rejects_invalid_depends_on_name():
    with pytest.raises(ValidationError):
        ServerSpec(**_server("web-1", depends_on=["../etc"]))


# --- load_stack / resolve_stack ---


def test_load_stack_fills_stack_and_orders_topologically():
    stack = load_stack(STACK_YAML)

    assert stack.name == "agents"
    # dns-1 は依存される側なので先頭。依存関係が決めない順序はファイルの順を保つ。
    assert list(stack.servers) == ["dns-1", "agent-1", "agent-2"]
    assert {s["stack"] for s in stack.servers.values()} == {"agents"}
    assert stack.servers["dns-1"]["hostname"] == "dns-1"


def test_resolve_stack_accepts_matching_explicit_stack():
    stack = _stack(_server("dns-1", stack="agents"))
    assert stack.servers["dns-1"]["stack"] == "agents"


def test_resolve_stack_rejects_other_stack_label():
    with pytest.raises(StackError, match="infra"):
        _stack(_server("dns-1", stack="infra"))


def test_resolve_stack_rejects_duplicate_names():
    with pytest.raises(StackError, match="重複"):
        _stack(_server("dns-1"), _server("dns-1"))


def test_resolve_stack_rejects_cycle_with_path():
    with pytest.raises(StackError, match="a -> b -> a"):
        _stack(
            _server("c", depends_on=["a"]),
            _server("a", depends_on=["b"]),
            _server("b", depends_on=["a"]),
        )


def test_resolve_stack_allows_reference_outside_stack():
    """スタック外の参照は既存 VM かもしれないので、読み込みでは拒否しない。"""
    stack = _stack(_server("agent-1", depends_on=["router-1"]))
    assert list(stack.servers) == ["agent-1"]


def test_load_stack_rejects_empty_servers():
    with pytest.raises(ValidationError):
        load_stack("stack: agents\nservers: []\n")


def test_load_stack_rejects_invalid_server_spec():
    with pytest.raises(ValidationError):
        load_stack("stack: agents\nservers:\n  - name: dns-1\n")


# --- compute_plan ---


def test_compute_plan_creates_absent_servers_in_order():
    stack = load_stack(STACK_YAML)

    plan = compute_plan(stack, {})

    assert [(i.name, i.action) for i in plan.items] == [
        ("dns-1", "create"),
        ("agent-1", "create"),
        ("agent-2", "create"),
    ]
    assert plan.blockers == []


def test_compute_plan_classifies_existing_servers():
    stack = _stack(
        _server("a"),
        _server("b", memory=2048),
        _server("c", disk=20),
        _server("d", vcpus=4),
    )
    managed = {
        "a": _observed(_server("a", stack="agents")),
        "b": _observed(_server("b", stack="agents"), state="shutoff"),
        "c": _observed(_server("c", stack="agents")),
        "d": _observed(_server("d", stack="agents"), state="running"),
    }

    plan = compute_plan(stack, managed)

    by_name = {i.name: i for i in plan.items}
    assert by_name["a"].action == "noop"
    assert by_name["b"].action == "converge"
    assert by_name["b"].fields == ("memory",)
    assert by_name["c"].action == "conflict"
    assert by_name["c"].recreate_fields == ("disk",)
    assert by_name["d"].action == "blocked_running"
    assert by_name["d"].offline_fields == ("vcpus",)
    assert [i.name for i in plan.blockers] == ["c", "d"]


def test_compute_plan_adopts_unlabeled_vm_as_live_change():
    """stack ラベルの無い既存 VM は、稼働中でも metadata の書き換えだけで取り込める。"""
    stack = _stack(_server("dns-1"))
    managed = {"dns-1": _observed(_server("dns-1"), state="running")}

    item = compute_plan(stack, managed).items[0]

    assert item.action == "converge"
    assert item.fields == ("stack",)


def test_compute_plan_rejects_taking_vm_from_other_stack():
    stack = _stack(_server("dns-1"))
    managed = {"dns-1": _observed(_server("dns-1", stack="infra"))}

    item = compute_plan(stack, managed).items[0]

    assert item.action == "conflict"
    assert "infra" in item.reason


def test_compute_plan_accepts_dependency_on_existing_managed_vm():
    stack = _stack(_server("agent-1", depends_on=["router-1"]))

    plan = compute_plan(stack, {"router-1": None})

    assert [i.action for i in plan.items] == ["create"]


def test_compute_plan_rejects_unknown_dependency():
    stack = _stack(_server("agent-1", depends_on=["router-1"]))

    with pytest.raises(StackError, match="router-1"):
        compute_plan(stack, {})


def test_compute_plan_prunes_same_stack_vms_in_reverse_order():
    stack = _stack(_server("dns-1"))
    managed = {
        "dns-1": _observed(_server("dns-1", stack="agents")),
        "old-db": _observed(_server("old-db", stack="agents")),
        "old-app": _observed(_server("old-app", stack="agents", depends_on=["old-db"])),
        "other": _observed(_server("other", stack="infra")),
        "loose": _observed(_server("loose")),
    }

    plan = compute_plan(stack, managed, prune=True)

    assert [(i.name, i.action) for i in plan.items] == [
        ("dns-1", "noop"),
        ("old-app", "delete"),
        ("old-db", "delete"),
    ]


def test_compute_plan_without_prune_keeps_same_stack_vms():
    stack = _stack(_server("dns-1"))
    managed = {
        "dns-1": _observed(_server("dns-1", stack="agents")),
        "old-db": _observed(_server("old-db", stack="agents")),
    }

    assert [i.name for i in compute_plan(stack, managed).items] == ["dns-1"]


def test_compute_plan_rejects_pruning_vm_still_depended_on():
    stack = _stack(_server("agent-1", depends_on=["dns-1"]))
    managed = {"dns-1": _observed(_server("dns-1", stack="agents"))}

    with pytest.raises(StackError, match="dns-1"):
        compute_plan(stack, managed, prune=True)


def test_compute_plan_rejects_pruning_vm_depended_on_by_other_vm():
    stack = _stack(_server("agent-1"))
    managed = {
        "dns-1": _observed(_server("dns-1", stack="agents")),
        "web-1": _observed(_server("web-1", depends_on=["dns-1"])),
    }

    with pytest.raises(StackError, match="web-1"):
        compute_plan(stack, managed, prune=True)


def test_plan_to_dict_omits_empty_details():
    stack = _stack(_server("a"), _server("b", memory=2048))
    managed = {"b": _observed(_server("b", stack="agents"), state="shutoff")}

    assert compute_plan(stack, managed).to_dict() == {
        "stack": "agents",
        "changes": [
            {"name": "a", "action": "create"},
            {
                "name": "b",
                "action": "converge",
                "fields": ["memory"],
                "offline_fields": ["memory"],
            },
        ],
    }


# --- plan_stack ---


def test_plan_stack_reads_only_stack_servers_without_prune():
    mgr = _fake_manager(
        {
            "dns-1": _observed(_server("dns-1", stack="agents")),
            "unrelated": _observed(_server("unrelated")),
        }
    )

    plan = plan_stack(mgr, _stack(_server("dns-1")))

    assert [i.action for i in plan.items] == ["noop"]
    mgr.get.assert_called_once_with("dns-1")
    mgr.create.assert_not_called()


def test_plan_stack_normalizes_old_metadata_spec():
    """フィールド追加前の metadata(stack/depends_on 無し)も差分扱いにしない。"""
    old = ServerSpec(**_server("dns-1", stack="agents")).model_dump()
    del old["depends_on"]
    mgr = _fake_manager({"dns-1": {"spec": old, "status": {"state": "running"}}})

    plan = plan_stack(mgr, _stack(_server("dns-1")))

    assert plan.items[0].action == "noop"


def test_plan_stack_skips_vm_deleted_between_list_and_get():
    mgr = _fake_manager()
    mgr.list.side_effect = lambda: ["gone"]

    plan = plan_stack(mgr, _stack(_server("dns-1")), prune=True)

    assert [(i.name, i.action) for i in plan.items] == [("dns-1", "create")]


# --- apply_stack ---


def test_apply_stack_creates_in_topological_order():
    mgr = _fake_manager()

    result = apply_stack(mgr, load_stack(STACK_YAML))

    assert _write_calls(mgr) == [
        ("create", "dns-1"),
        ("create", "agent-1"),
        ("create", "agent-2"),
    ]
    assert [c["action"] for c in result["changes"]] == ["create"] * 3
    assert result["changes"][0]["status"] == {
        "state": "running",
        "ip": "192.168.122.10",
    }


def test_apply_stack_refuses_blockers_without_changes():
    mgr = _fake_manager(
        {
            "dns-1": _observed(_server("dns-1", stack="agents")),
            "old": _observed(_server("old", stack="agents")),
        }
    )
    stack = _stack(_server("new-1"), _server("dns-1", disk=20))

    with pytest.raises(
        StackError, match=r"何も変更していません: dns-1 \(conflict: disk\)"
    ):
        apply_stack(mgr, stack, prune=True)

    assert _write_calls(mgr) == []


def test_apply_stack_deletes_pruned_vms_last_in_reverse_order():
    mgr = _fake_manager(
        {
            "old-db": _observed(_server("old-db", stack="agents")),
            "old-app": _observed(
                _server("old-app", stack="agents", depends_on=["old-db"])
            ),
        }
    )

    result = apply_stack(mgr, _stack(_server("dns-1")), prune=True)

    assert _write_calls(mgr) == [
        ("create", "dns-1"),
        ("delete", "old-app"),
        ("delete", "old-db"),
    ]
    assert result["changes"][-1] == {"name": "old-db", "action": "delete"}


def test_apply_stack_passes_secrets_per_server():
    mgr = _fake_manager()
    stack = _stack(_server("a"), _server("b"))

    apply_stack(mgr, stack, secrets={"b": {"TOKEN": "s3cret"}})

    assert mgr.create.call_args_list[0].kwargs == {"secrets": None}
    assert mgr.create.call_args_list[1].kwargs == {"secrets": {"TOKEN": "s3cret"}}


def test_apply_stack_rejects_secrets_for_unknown_server():
    mgr = _fake_manager()

    with pytest.raises(StackError, match="typo"):
        apply_stack(mgr, _stack(_server("a")), secrets={"typo": {"K": "v"}})

    mgr.create.assert_not_called()


def test_apply_stack_reports_applied_and_pending_on_failure():
    mgr = _fake_manager()
    original = mgr.create.side_effect

    def _create(spec, secrets=None):
        if spec["name"] == "agent-1":
            raise ServerConflict("agent-1")
        return original(spec, secrets)

    mgr.create.side_effect = _create

    with pytest.raises(StackError) as excinfo:
        apply_stack(mgr, load_stack(STACK_YAML))

    err = excinfo.value
    assert err.applied == ["dns-1"]
    assert err.pending == ["agent-1", "agent-2"]
    assert "server conflict: agent-1" in str(err)
    assert isinstance(err.__cause__, ServerConflict)
    # 失敗した後の VM には進まない。
    assert _write_calls(mgr) == [("create", "dns-1"), ("create", "agent-1")]


def test_apply_stack_does_not_log_secrets(caplog):
    mgr = _fake_manager()
    mgr.create.side_effect = RuntimeError("boom")

    with caplog.at_level("DEBUG", logger="mini_vps"):
        with pytest.raises(StackError) as excinfo:
            apply_stack(
                mgr,
                _stack(_server("a")),
                secrets={"a": {"AI_ENGINE_TOKEN": "sk-super-secret"}},
            )

    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert "sk-super-secret" not in emitted
    assert "AI_ENGINE_TOKEN" not in emitted
    assert "sk-super-secret" not in str(excinfo.value)


# --- wait ---


class _FakeClock:
    """sleep で進む時計。テストで実時間を待たないために使う。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _status_sequence(mgr, name, statuses):
    """mgr.status(name) が statuses を順に返すようにする(最後の値を繰り返す)。"""
    remaining = list(statuses)
    fallback = mgr.status.side_effect

    def _status(n):
        if n != name:
            return fallback(n)
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    mgr.status.side_effect = _status


def test_wait_until_ready_polls_until_running_with_ip():
    mgr = MagicMock()
    _status_sequence(
        mgr,
        "dns-1",
        [
            {"state": "running", "ip": None},
            {"state": "running", "ip": None},
            {"state": "running", "ip": "192.168.122.53"},
        ],
    )
    clock = _FakeClock()

    status = wait_until_ready(mgr, "dns-1", 60, clock=clock, sleep=clock.sleep)

    assert status["ip"] == "192.168.122.53"
    assert clock.sleeps == [2.0, 2.0]


def test_wait_until_ready_times_out():
    mgr = MagicMock()
    mgr.status.return_value = {"state": "running", "ip": None}
    clock = _FakeClock()

    with pytest.raises(StackError, match="10 秒以内に起動しませんでした"):
        wait_until_ready(mgr, "dns-1", 10, clock=clock, sleep=clock.sleep)

    assert clock.now >= 10


def test_wait_until_ready_fails_fast_when_stopped():
    mgr = MagicMock()
    mgr.status.return_value = {"state": "shutoff", "ip": None}
    clock = _FakeClock()

    with pytest.raises(StackError, match="停止"):
        wait_until_ready(mgr, "dns-1", 60, clock=clock, sleep=clock.sleep)

    assert clock.sleeps == []


def test_wait_until_ready_needs_only_running_on_user_mode_network():
    """user-mode ネットワーク(macOS)では IP を得られないため running で足りる。"""
    set_profile(macos_profile())
    mgr = MagicMock()
    mgr.status.return_value = {"state": "running", "ip": None}
    clock = _FakeClock()

    wait_until_ready(mgr, "dns-1", 60, clock=clock, sleep=clock.sleep)

    assert clock.sleeps == []


def test_apply_stack_waits_for_dependencies_before_dependents():
    mgr = _fake_manager()
    original_create = mgr.create.side_effect

    def _create(spec, secrets=None):
        # 作成直後は IP がまだ無い状態を再現する。
        result, created = original_create(spec, secrets)
        result["status"]["ip"] = None
        return result, created

    mgr.create.side_effect = _create
    polled = []
    original_status = mgr.status.side_effect

    def _status(name):
        polled.append(name)
        status = original_status(name)
        if polled.count(name) >= 2:
            status["ip"] = "192.168.122.53"
        return status

    mgr.status.side_effect = _status
    clock = _FakeClock()

    apply_stack(mgr, load_stack(STACK_YAML), wait=True, clock=clock, sleep=clock.sleep)

    # dns-1 の起動は1度だけ待ち、agent-1 の作成前に確認している。
    assert polled == ["dns-1", "dns-1"]
    calls = [
        (c[0], c.args[0] if c[0] == "status" else c.args[0]["name"])
        for c in mgr.mock_calls
        if c[0] in ("status", "create")
    ]
    assert calls == [
        ("create", "dns-1"),
        ("status", "dns-1"),
        ("status", "dns-1"),
        ("create", "agent-1"),
        ("create", "agent-2"),
    ]


def test_apply_stack_without_wait_does_not_poll():
    mgr = _fake_manager()

    apply_stack(mgr, load_stack(STACK_YAML))

    mgr.status.assert_not_called()


def test_apply_stack_wait_timeout_reports_progress():
    mgr = _fake_manager()
    mgr.status.side_effect = lambda name: {"state": "running", "ip": None}
    clock = _FakeClock()

    with pytest.raises(StackError) as excinfo:
        apply_stack(
            mgr,
            load_stack(STACK_YAML),
            wait=True,
            wait_timeout=5,
            clock=clock,
            sleep=clock.sleep,
        )

    assert excinfo.value.applied == ["dns-1"]
    assert excinfo.value.pending == ["agent-1", "agent-2"]
    assert "dns-1" in str(excinfo.value)


# --- CLI ---


def _factory(mgr):
    return lambda: contextlib.nullcontext(mgr)


@pytest.fixture
def stack_file(tmp_path):
    path = tmp_path / "stack.yaml"
    path.write_text(STACK_YAML)
    return str(path)


def test_cli_plan_prints_changes(stack_file, capsys):
    mgr = _fake_manager()

    exit_code = cli.main(["plan", stack_file], manager_factory=_factory(mgr))

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert [c["name"] for c in output["changes"]] == ["dns-1", "agent-1", "agent-2"]
    mgr.create.assert_not_called()


def test_cli_plan_passes_prune(stack_file, capsys):
    mgr = _fake_manager({"old": _observed(_server("old", stack="agents"))})

    exit_code = cli.main(["plan", stack_file, "--prune"], manager_factory=_factory(mgr))

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["changes"][-1] == {"name": "old", "action": "delete"}


def test_cli_apply_passes_per_server_startup_params(stack_file, capsys):
    mgr = _fake_manager()

    exit_code = cli.main(
        [
            "apply",
            stack_file,
            "--startup-param",
            "agent-1:TOKEN=a=b",
            "--startup-param",
            "agent-1:OTHER=x",
        ],
        manager_factory=_factory(mgr),
    )

    assert exit_code == 0
    by_name = {c.args[0]["name"]: c.kwargs["secrets"] for c in mgr.create.mock_calls}
    assert by_name == {
        "dns-1": None,
        "agent-1": {"TOKEN": "a=b", "OTHER": "x"},
        "agent-2": None,
    }
    assert "a=b" not in capsys.readouterr().out


def test_cli_apply_rejects_startup_param_without_server(stack_file, capsys):
    mgr = _fake_manager()

    exit_code = cli.main(
        ["apply", stack_file, "--startup-param", "TOKEN=s3cret"],
        manager_factory=_factory(mgr),
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "SERVER:KEY=VALUE" in err
    assert "s3cret" not in err
    mgr.create.assert_not_called()


def test_cli_apply_returns_exit_code_12_on_stack_error(tmp_path, capsys):
    path = tmp_path / "stack.yaml"
    path.write_text(STACK_YAML.replace("depends_on: [dns-1]", "depends_on: [nope]"))
    mgr = _fake_manager()

    exit_code = cli.main(["apply", str(path)], manager_factory=_factory(mgr))

    assert exit_code == 12
    assert "stack error" in capsys.readouterr().err


def test_cli_plan_returns_exit_code_1_on_invalid_yaml(tmp_path, capsys):
    path = tmp_path / "stack.yaml"
    path.write_text("stack: agents\nservers:\n  - name: dns-1\n")

    exit_code = cli.main(["plan", str(path)], manager_factory=_factory(MagicMock()))

    assert exit_code == 1


def test_cli_apply_passes_wait_options(stack_file, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        cli,
        "apply_stack",
        lambda mgr, stack, **kwargs: captured.update(kwargs) or {"changes": []},
    )

    exit_code = cli.main(
        ["apply", stack_file, "--wait", "--wait-timeout", "30", "--prune"],
        manager_factory=_factory(MagicMock()),
    )

    assert exit_code == 0
    assert captured == {
        "prune": True,
        "wait": True,
        "secrets": {},
        "wait_timeout": 30.0,
    }


# --- API ---


@pytest.fixture
def client(monkeypatch):
    mgr = _fake_manager()
    monkeypatch.setattr("mini_vps.api.libvirt.open", lambda uri: MagicMock())
    api_module.app.dependency_overrides[api_module.get_manager] = lambda: mgr
    with TestClient(api_module.app) as test_client:
        yield test_client, mgr
    api_module.app.dependency_overrides.clear()


STACK_BODY = {
    "stack": "agents",
    "servers": [
        _server("dns-1"),
        _server("agent-1", depends_on=["dns-1"]),
    ],
}


def test_api_plan_returns_changes(client):
    test_client, mgr = client

    response = test_client.post("/stacks/plan", json=STACK_BODY)

    assert response.status_code == 200
    assert response.json() == {
        "stack": "agents",
        "changes": [
            {"name": "dns-1", "action": "create"},
            {"name": "agent-1", "action": "create"},
        ],
    }
    mgr.create.assert_not_called()


def test_api_plan_returns_422_on_cycle(client):
    test_client, _ = client
    body = {
        "stack": "agents",
        "servers": [
            _server("a", depends_on=["b"]),
            _server("b", depends_on=["a"]),
        ],
    }

    response = test_client.post("/stacks/plan", json=body)

    assert response.status_code == 422
    assert response.json()["detail"].startswith("stack error: ")


def test_api_apply_passes_secrets_without_echoing(client):
    test_client, mgr = client
    body = {**STACK_BODY, "secrets": {"agent-1": {"TOKEN": "sk-super-secret"}}}

    response = test_client.post("/stacks/apply", json=body)

    assert response.status_code == 200
    assert "sk-super-secret" not in response.text
    assert mgr.create.call_args_list == [
        call(
            ServerSpec(**_server("dns-1", stack="agents")).model_dump(),
            secrets=None,
        ),
        call(
            ServerSpec(
                **_server("agent-1", stack="agents", depends_on=["dns-1"])
            ).model_dump(),
            secrets={"TOKEN": "sk-super-secret"},
        ),
    ]


def test_api_apply_returns_422_on_conflict_without_changes(client):
    test_client, mgr = client
    mgr.create(ServerSpec(**_server("dns-1", stack="agents")).model_dump())
    mgr.create.reset_mock()
    body = {**STACK_BODY, "servers": [_server("dns-1", disk=50)]}

    response = test_client.post("/stacks/apply", json=body)

    assert response.status_code == 422
    assert "dns-1 (conflict: disk)" in response.json()["detail"]
    mgr.create.assert_not_called()


def test_api_apply_rejects_non_positive_wait_timeout(client):
    test_client, _ = client

    response = test_client.post("/stacks/apply", json={**STACK_BODY, "wait_timeout": 0})

    assert response.status_code == 422


def test_logging_is_not_configured_by_stack_module():
    """ライブラリ層はロガーを持つだけで、ハンドラを付けない。"""
    assert logging.getLogger("mini_vps.stack").handlers == []
