import json

import pytest

from mini_vps.startup_scripts import (
    STARTUP_SCRIPT_NAMES,
    StartupScriptError,
    render_startup_script,
)


def _spec(**overrides):
    spec = {"name": "web-1", "user": "ubuntu"}
    spec.update(overrides)
    return spec


# --- render_startup_script(unknown name) ---


def test_render_startup_script_rejects_unknown_name():
    with pytest.raises(StartupScriptError):
        render_startup_script("no-such-template", _spec(), {"AI_ENGINE_TOKEN": "x"})


# --- opencode-sakura-ai-engine: secrets 検証 ---


def test_render_opencode_sakura_ai_engine_requires_token():
    with pytest.raises(StartupScriptError):
        render_startup_script("opencode-sakura-ai-engine", _spec(), {})


def test_render_opencode_sakura_ai_engine_rejects_empty_token():
    with pytest.raises(StartupScriptError):
        render_startup_script(
            "opencode-sakura-ai-engine", _spec(), {"AI_ENGINE_TOKEN": ""}
        )


def test_render_opencode_sakura_ai_engine_rejects_none_secrets():
    with pytest.raises(StartupScriptError):
        render_startup_script("opencode-sakura-ai-engine", _spec(), None)


# --- opencode-sakura-ai-engine: 生成内容 ---


def _render(user="ubuntu", token="sk-test-token"):
    return render_startup_script(
        "opencode-sakura-ai-engine", _spec(user=user), {"AI_ENGINE_TOKEN": token}
    )


def _opencode_json_content(fragment):
    (entry,) = [
        f for f in fragment["write_files"] if f["path"].endswith("opencode.json")
    ]
    return json.loads(entry["content"])


def test_render_opencode_sakura_ai_engine_apikey_placeholder_is_literal():
    config = _opencode_json_content(_render())
    provider = config["provider"]["sakura-ai-engine"]
    # "{env:AI_ENGINE_TOKEN}" は opencode 自身が解釈するプレースホルダであり、
    # 実トークンの値そのものに置換されてはいけない(.format() 混入の回帰防止)。
    assert provider["options"]["apiKey"] == "{env:AI_ENGINE_TOKEN}"
    assert provider["options"]["baseURL"] == "https://api.ai.sakura.ad.jp/v1"
    assert provider["npm"] == "@ai-sdk/openai-compatible"


def test_render_opencode_sakura_ai_engine_registers_default_and_preview_models():
    config = _opencode_json_content(_render())
    models = config["provider"]["sakura-ai-engine"]["models"]
    assert "gpt-oss-120b" in models
    assert "preview/Kimi-K2.6" in models
    assert config["model"] == "sakura-ai-engine/gpt-oss-120b"


def test_render_opencode_sakura_ai_engine_token_file_has_0600_permissions():
    fragment = _render(token="sk-secret-value")
    (entry,) = [
        f for f in fragment["write_files"] if f["path"].endswith("ai-engine-token.env")
    ]
    assert entry["permissions"] == "0600"
    assert "sk-secret-value" in entry["content"]


@pytest.mark.parametrize("user", ["ubuntu", "deploy"])
def test_render_opencode_sakura_ai_engine_runcmd_uses_spec_user(user):
    fragment = _render(user=user)
    runcmd_text = "\n".join(fragment["runcmd"])
    assert f"chown -R {user}:{user}" in runcmd_text
    assert f"sudo -u {user} -H bash" in runcmd_text
    assert f"/home/{user}/.config/opencode" in runcmd_text


def test_render_opencode_sakura_ai_engine_installs_opencode():
    fragment = _render()
    runcmd_text = "\n".join(fragment["runcmd"])
    assert "curl -fsSL https://opencode.ai/install | bash" in runcmd_text


def test_startup_script_names_contains_opencode_sakura_ai_engine():
    assert "opencode-sakura-ai-engine" in STARTUP_SCRIPT_NAMES
