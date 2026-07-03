"""`python -m mini_vps` のエントリポイント。実体は `cli.run` に委譲する。"""

from .cli import run

if __name__ == "__main__":
    run()
