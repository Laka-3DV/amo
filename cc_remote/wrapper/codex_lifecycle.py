"""Payload-free native lifecycle proof for desktop-owned Codex rollouts.

An independent stdio server reports desktop threads as notLoaded and may
persist interrupted at a compaction boundary. Only task_started/task_complete/
turn_aborted identify the actual task lifetime. Never infer it from recency,
assistant text, or the presence of a compacted record.
"""
from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024


@dataclass
class Lifecycle:
    active: str | None = None
    terminals: OrderedDict = field(default_factory=OrderedDict)
    stamp: tuple | None = None
    offset: int = 0
    complete: bool = True


class NativeLifecycle:
    def __init__(self, home: Path | None = None):
        self.home = (home or Path(os.environ.get("CODEX_HOME") or Path.home()/".codex")).resolve()
        self.cache: OrderedDict[str, Lifecycle] = OrderedDict()
        self.lock = threading.RLock()

    def source(self, thread: dict) -> Path | None:
        value = thread.get("path")
        if not isinstance(value, str):
            return None
        path = Path(value).resolve()
        roots = [self.home/"sessions", self.home/"archived_sessions"]
        if path.suffix != ".jsonl" or not any(path.is_relative_to(root) for root in roots):
            return None
        return path

    def read(self, thread: dict) -> Lifecycle | None:
        with self.lock:
            return self._read(thread)

    def _read(self, thread: dict) -> Lifecycle | None:
        path = self.source(thread)
        if path is None:
            return None
        try:
            stat = path.stat()
            if not path.is_file() or stat.st_size > MAX_SOURCE_BYTES:
                return None
            key = str(path)
            state = self.cache.get(key)
            identity = (stat.st_dev, stat.st_ino)
            stamp = (*identity, stat.st_size, stat.st_mtime_ns)
            if state and state.stamp == stamp:
                self.cache.move_to_end(key)
                return Lifecycle(state.active, OrderedDict(state.terminals), stamp, state.offset, state.complete)
            if not state or state.stamp[:2] != identity or stat.st_size < state.offset:
                state = Lifecycle()
            with path.open("rb") as stream:
                stream.seek(state.offset)
                while True:
                    start = stream.tell()
                    raw = stream.readline(MAX_RECORD_BYTES + 1)
                    if not raw:
                        break
                    if len(raw) > MAX_RECORD_BYTES:
                        # Never make a write-ownership decision from a source
                        # whose lifecycle could not be completely inspected.
                        state.complete = False
                        while raw and not raw.endswith(b"\n"):
                            raw = stream.readline(MAX_RECORD_BYTES + 1)
                        state.offset = stream.tell()
                        continue
                    if not raw.endswith(b"\n"):
                        state.offset = start  # retry an unfinished append
                        break
                    state.offset = stream.tell()
                    if not any(marker in raw for marker in (b'"task_started"', b'"task_complete"', b'"turn_aborted"')):
                        continue
                    try:
                        row = json.loads(raw)
                    except (ValueError, UnicodeError):
                        state.complete = False
                        continue
                    if row.get("type") != "event_msg":
                        continue
                    event = row.get("payload") or {}
                    turn = event.get("turn_id")
                    if not isinstance(turn, str) or not turn or len(turn) > 256:
                        continue
                    kind = event.get("type")
                    if kind == "task_started":
                        state.active = turn
                        state.terminals.pop(turn, None)
                    elif kind in {"task_complete", "turn_aborted"}:
                        if state.active == turn:
                            state.active = None
                        state.terminals[turn] = ("interrupted" if kind == "turn_aborted"
                                                 else "failed" if event.get("error") else "completed")
                        if len(state.terminals) > 512:
                            state.terminals.popitem(last=False)
            state.stamp = stamp
            self.cache[key] = state
            self.cache.move_to_end(key)
            if len(self.cache) > 128:
                self.cache.popitem(last=False)
            # Return a snapshot: the next incremental read must not mutate a
            # history projection already being built on the asyncio loop.
            return Lifecycle(state.active, OrderedDict(state.terminals), stamp, state.offset, state.complete)
        except (OSError, ValueError):
            return None


def apply_lifecycle(turn: dict, proof: Lifecycle | None) -> dict:
    if proof is None or not proof.complete:
        return turn
    identity = turn.get("id")
    status = "inProgress" if proof.active == identity else proof.terminals.get(identity)
    if status is None or status == turn.get("status"):
        return turn
    return {**turn, "status": status, "error": None if status != "failed" else turn.get("error")}
