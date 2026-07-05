"""VM 初回起動時のセットアップを自動化する、名前付き cloud-init テンプレート。

テンプレート名から write_files/runcmd の cloud-init フラグメントを組み立てる。
秘密情報(secrets)はここでのみ user-data の平文へ展開され、呼び出し側
(manager.py の _write_spec)には一切渡らないことが本モジュールの前提。
"""

import json

_REQUIRED_TOKEN_KEY = "AI_ENGINE_TOKEN"
_STAGING_DIR = "/root/minivps-startup"


class StartupScriptError(ValueError):
    """未知の startup_script 名、または必須 secrets キー欠落を表す。"""


def _render_opencode_sakura_ai_engine(spec: dict, secrets: dict[str, str]) -> dict:
    """opencode-sakura-ai-engine テンプレートの cloud-init フラグメントを組み立てる。

    OpenCode(ターミナル向けAIコーディングエージェント)をインストールし、
    さくらのAI Engineをカスタム OpenAI 互換プロバイダとして登録する。

    cloud-init は write-files モジュールが users-groups モジュールより先に走るため、
    対象ユーザーはまだ存在しない。よって root 所有のステージング先に書き出し、
    runcmd(ユーザー作成後)で対象ユーザーのホームへ移動・chown する。

    Raises:
        StartupScriptError: secrets に AI_ENGINE_TOKEN が無い、または空文字の場合。
    """
    token = secrets.get(_REQUIRED_TOKEN_KEY)
    if not token:
        raise StartupScriptError(
            f"startup_script 'opencode-sakura-ai-engine' には "
            f"secrets['{_REQUIRED_TOKEN_KEY}'] が必須です"
        )

    user = spec["user"]
    home_dir = f"/home/{user}/.config/opencode"

    # "{env:AI_ENGINE_TOKEN}" は OpenCode 自身が解釈するプレースホルダ文字列であり、
    # Python 側の文字列フォーマットには一切通さない(dict リテラル→json.dumps のみ)。
    opencode_config = {
        "$schema": "https://opencode.ai/config.json",
        "model": "sakura-ai-engine/gpt-oss-120b",
        "provider": {
            "sakura-ai-engine": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Sakura AI Engine",
                "options": {
                    "baseURL": "https://api.ai.sakura.ad.jp/v1",
                    "apiKey": "{env:AI_ENGINE_TOKEN}",
                },
                "models": {
                    "gpt-oss-120b": {"name": "gpt-oss 120b (Sakura AI Engine)"},
                    "preview/Kimi-K2.6": {
                        "name": "Kimi K2.6 [preview] (Sakura AI Engine)"
                    },
                },
            }
        },
    }

    write_files = [
        {
            "path": f"{_STAGING_DIR}/opencode.json",
            "permissions": "0644",
            "content": json.dumps(opencode_config, indent=2) + "\n",
        },
        {
            "path": f"{_STAGING_DIR}/ai-engine-token.env",
            "permissions": "0600",
            "content": f"AI_ENGINE_TOKEN={token}\n",
        },
    ]
    runcmd = [
        f"mkdir -p {home_dir}",
        f"mv {_STAGING_DIR}/opencode.json {home_dir}/opencode.json",
        f"mv {_STAGING_DIR}/ai-engine-token.env {home_dir}/ai-engine-token.env",
        f"chown -R {user}:{user} {home_dir}",
        f"chmod 0600 {home_dir}/ai-engine-token.env",
        (
            f'echo \'[ -f "$HOME/.config/opencode/ai-engine-token.env" ] && '
            f'set -a && . "$HOME/.config/opencode/ai-engine-token.env" && set +a\' '
            f">> /home/{user}/.bashrc"
        ),
        f'sudo -u {user} -H bash -c "curl -fsSL https://opencode.ai/install | bash"',
    ]
    return {"write_files": write_files, "runcmd": runcmd}


_RENDERERS = {
    "opencode-sakura-ai-engine": _render_opencode_sakura_ai_engine,
}

STARTUP_SCRIPT_NAMES = frozenset(_RENDERERS)


def render_startup_script(
    name: str, spec: dict, secrets: dict[str, str] | None
) -> dict:
    """テンプレート名から cloud-init フラグメント(write_files/runcmd)を組み立てる。

    secrets の None は空 dict として扱う。

    Raises:
        StartupScriptError: 未知のテンプレート名、または必須 secrets キー欠落の場合。
    """
    try:
        renderer = _RENDERERS[name]
    except KeyError:
        raise StartupScriptError(
            f"unknown startup_script: {name!r} (known: {sorted(_RENDERERS)})"
        ) from None
    return renderer(spec, secrets or {})
