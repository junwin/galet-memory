"""Small file-backed store with the same explicit contracts as SQLite.

A complete JSONL snapshot is atomically replaced under a cross-process lock.
Every event carries its correlations, so a write cannot leave orphaned links.
"""

from __future__ import annotations
import json
import os
import secrets
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import Session, Event, EpisodicCompatibilityError
from .store import EpisodicStore
from .file_lock import StoreFileLock


class JsonlEpisodicMemory(EpisodicStore):
    def __init__(self, root: str | Path, *, digests_root=None, digest_search=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "episodic-v2.jsonl"
        self.digests_root = Path(digests_root) if digests_root is not None else None
        self.digest_search = digest_search
        self._lock = StoreFileLock(self.root)
        self._depth = 0
        with self._lock:
            if not self.path.exists():
                if any(p.name != ".episodic.lock" for p in self.root.iterdir()):
                    raise EpisodicCompatibilityError(
                        "unsupported episodic directory; use a fresh directory"
                    )
                self._sessions, self._events = {}, {}
                self._sequence = 0
                self._cursor_key = secrets.token_bytes(32)
                self._save()
            self._load()

    def _load(self):
        try:
            with self.path.open(encoding="utf-8") as stream:
                header = json.loads(next(stream))
                if header["version"] != 2:
                    raise ValueError("wrong version")
                self._sequence = header["sequence"]
                self._cursor_key = bytes.fromhex(header["cursor_key"])
                self._sessions, self._events = {}, {}
                for line in stream:
                    record = json.loads(line)
                    s = record["session"]
                    for key in ("created_at", "updated_at"):
                        s[key] = datetime.fromisoformat(s[key])
                    s["tags"] = tuple(s["tags"])
                    session = Session(**s)
                    events = []
                    for e in record["events"]:
                        for key in ("created_at", "stored_at"):
                            e[key] = datetime.fromisoformat(e[key])
                        e["correlation_ids"] = tuple(e["correlation_ids"])
                        events.append(Event(**e))
                    self._sessions[session.session_id] = session
                    self._events[session.session_id] = events
        except (ValueError, KeyError, TypeError, StopIteration) as exc:
            raise EpisodicCompatibilityError(
                "incompatible JSONL store; use a fresh directory"
            ) from exc

    def _save(self):
        temp = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.root, delete=False
            ) as stream:
                temp = Path(stream.name)
                stream.write(
                    json.dumps(
                        {
                            "version": 2,
                            "sequence": self._sequence,
                            "cursor_key": self._cursor_key.hex(),
                        }
                    )
                    + "\n"
                )
                for sid, s in self._sessions.items():
                    stream.write(
                        json.dumps(
                            {
                                "session": asdict(s),
                                "events": [
                                    asdict(e) for e in self._events.get(sid, [])
                                ],
                            },
                            default=lambda v: v.isoformat(),
                        )
                        + "\n"
                    )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            if os.name != "nt":
                descriptor = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    @contextmanager
    def _transaction(self, write=False):
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._load()
            self._depth += 1
            try:
                yield
                if outer and write:
                    self._save()
            finally:
                self._depth -= 1
                # On failure no file replacement occurred. The next outer call
                # reloads committed state rather than using partial memory state.

    def _load_session(self, sid):
        return self._sessions.get(sid)

    def _all_sessions(self):
        return list(self._sessions.values())

    def _write_session(self, s):
        self._sessions[s.session_id] = s

    def _load_events(self, sid):
        return list(self._events.get(sid, ()))

    def _insert_events(self, sid, events):
        result = []
        for e in events:
            self._sequence += 1
            result.append(replace(e, sequence=self._sequence))
        self._events.setdefault(sid, []).extend(result)
        return result

    def _tail(self, sid):
        es = self._events.get(sid, ())
        return es[-1].event_id if es else None

    def _max_sequence(self):
        return self._sequence

    def _delete_session(self, sid):
        self._sessions.pop(sid, None)
        self._events.pop(sid, None)

    def _clear_events(self, sid):
        original = self._events.get(sid, [])
        kept = [e for e in original if e.kind == "events_invalidated"]
        self._events[sid] = kept
        return len(original) - len(kept)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
