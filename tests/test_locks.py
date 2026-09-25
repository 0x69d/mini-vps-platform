import multiprocessing
import os
import stat
import threading
import time

from mini_vps.locks import NameLocks


def _hold_lock(lock_dir, name, ready, release):
    with NameLocks(lock_dir).hold(name):
        ready.set()
        release.wait(10)


def test_hold_serializes_across_processes(tmp_path):
    """別プロセスが保持中の name は、解放されるまで取得できない。"""
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_lock, args=(str(tmp_path), "web-1", ready, release))
    proc.start()
    try:
        assert ready.wait(10)
        timer = threading.Timer(0.3, release.set)
        released_after = time.monotonic() + 0.3
        timer.start()
        with NameLocks(str(tmp_path)).hold("web-1"):
            acquired_at = time.monotonic()
        # 別プロセスが解放するまで待たされていること(タイマーの誤差を許容する)
        assert acquired_at >= released_after - 0.05
    finally:
        release.set()
        proc.join(10)


def test_hold_does_not_block_other_names(tmp_path):
    locks = NameLocks(str(tmp_path))
    with locks.hold("web-1"):
        with locks.hold("web-2"):
            pass


def test_hold_falls_back_to_in_process_lock_when_dir_unusable(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    locks = NameLocks(str(blocker / "locks"))  # ファイルの下にはディレクトリを作れない

    with caplog.at_level("WARNING", logger="mini_vps.locks"):
        with locks.hold("web-1"):
            pass
        with locks.hold("web-1"):
            pass

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1  # 警告は一度だけ


def test_hold_without_lock_dir_uses_only_thread_lock():
    with NameLocks(None).hold("web-1"):
        pass


def test_lock_file_is_group_writable_despite_umask(tmp_path):
    old = os.umask(0o022)
    try:
        with NameLocks(str(tmp_path)).hold("web-1"):
            pass
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "web-1.lock").stat().st_mode) == 0o660
