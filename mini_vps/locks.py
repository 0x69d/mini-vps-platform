"""VM の name 単位で書き込み操作を直列化するロック。

CLI・Web API・MCP サーバは別プロセスで動くため、プロセス内の threading.Lock だけでは
同じ name への create と delete が並行したときの TOCTOU を防げない。そこで
`<lock_dir>/<name>.lock` への `fcntl.flock`(Linux・macOS 共通)を重ねる。

- 同じプロセス内のスレッド同士は threading.Lock で排他する(flock はファイル記述子
  単位のため、同じプロセスでも別に open すれば排他されるが、先に threading.Lock で
  並べておけば待ちの原因がログで区別しやすい)。
- 別プロセス同士は flock で排他する。プロセスが落ちればカーネルが解放するため、
  ロックファイルが残っても次の取得を妨げない。

ロックは非再帰。`ServerManager.create()` がロック内で `get()` を呼ぶ構造は、
読み取り系がロックを取らない限り成立する。
"""

import contextlib
import fcntl
import logging
import os
import threading

_LOGGER = logging.getLogger(__name__)


class NameLocks:
    """name ごとのプロセス内ロックとプロセス間ロックを貸し出す。

    Attributes:
        lock_dir: ロックファイルを置くディレクトリ。None ならプロセス間ロックを
            取らない。
    """

    def __init__(self, lock_dir: str | None):
        self.lock_dir = lock_dir
        self._thread_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._warned = False

    def _thread_lock(self, name: str) -> threading.Lock:
        """指定 name 専用の threading.Lock を返す(無ければ生成する)。"""
        with self._guard:
            return self._thread_locks.setdefault(name, threading.Lock())

    def _open_lock_file(self, name: str) -> int | None:
        """ロックファイルを開いて fd を返す。作れなければ警告して None を返す。

        lock_dir を作れない(権限が無い)環境でも VM 操作自体は止めない。その場合は
        プロセス内の直列化だけになることを一度だけ警告する。
        """
        if self.lock_dir is None:
            return None
        try:
            os.makedirs(self.lock_dir, exist_ok=True)
            return os.open(
                os.path.join(self.lock_dir, f"{name}.lock"),
                os.O_RDWR | os.O_CREAT,
                0o660,
            )
        except OSError as e:
            if not self._warned:
                _LOGGER.warning(
                    "ロックディレクトリ %s を使えないため、"
                    "プロセス間の直列化を行わない: %s",
                    self.lock_dir,
                    e.strerror,
                )
                self._warned = True
            return None

    @contextlib.contextmanager
    def hold(self, name: str):
        """指定 name のロックを取得して保持する。待たされた場合は DEBUG に残す。

        Yields:
            None。
        """
        lock = self._thread_lock(name)
        if not lock.acquire(blocking=False):
            _LOGGER.debug("%s: ロック待ち(同一プロセス)", name)
            lock.acquire()
        fd = None
        try:
            fd = self._open_lock_file(name)
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    _LOGGER.debug("%s: ロック待ち(別プロセス)", name)
                    fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fd is not None:
                # close で flock も解放される。
                os.close(fd)
            lock.release()
