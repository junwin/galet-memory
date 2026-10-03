"""Reentrant JSONL store lock, shared by instances and operating-system processes."""

import os
import threading
from pathlib import Path


class StoreFileLock:
    _registry_guard = threading.Lock()
    _registry: dict[Path, "StoreFileLock"] = {}

    def __new__(cls, root: Path):
        with cls._registry_guard:
            if root not in cls._registry:
                lock = super().__new__(cls)
                lock.path = root / ".episodic.lock"
                lock.thread_lock = threading.RLock()
                lock.depth = 0
                cls._registry[root] = lock
            return cls._registry[root]

    def __enter__(self):
        self.thread_lock.acquire()
        try:
            if self.depth == 0:
                self.stream = self.path.open("a+b")
                try:
                    if os.name == "nt":
                        import msvcrt
                        if self.stream.tell() == 0:
                            self.stream.write(b"\0")
                            self.stream.flush()
                        self.stream.seek(0)
                        msvcrt.locking(self.stream.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)
                except BaseException:
                    self.stream.close()
                    raise
            self.depth += 1
            return self
        except BaseException:
            self.thread_lock.release()
            raise

    def __exit__(self, *_):
        try:
            self.depth -= 1
            if self.depth == 0:
                try:
                    if os.name == "nt":
                        import msvcrt
                        self.stream.seek(0)
                        msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
                finally:
                    self.stream.close()
        finally:
            self.thread_lock.release()
