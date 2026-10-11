from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from cc_remote.protocol import (
    ListSessions,
    PinSession,
    PROTOCOL_VERSION,
    SessionInfo,
    SessionList,
    deserialize,
    serialize,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.session_pins import SessionPinStore, SessionPinStoreError
from tests.test_multisession import _mk_ctx, _mk_machine


def test_pin_command_and_session_info_roundtrip():
    command = deserialize(serialize(PinSession(
        session_id="session-1", pinned=True, engine="codex", space="work")))
    assert command.type == "pin_session"
    assert command.session_id == "session-1" and command.pinned is True
    info = deserialize(serialize(SessionList(
        engine="claude", sessions=[SessionInfo(
            session_id="session-1", pinned=True)])))
    assert info.sessions[0].pinned is True


def test_session_pin_store_persists_and_unpins(tmp_path):
    store = SessionPinStore(tmp_path)
    store.set_pinned("claude", "claude-1", True)
    store.set_pinned("codex", "codex-1", True)
    assert SessionPinStore(tmp_path).ids("claude") == {"claude-1"}
    assert SessionPinStore(tmp_path).ids("codex") == {"codex-1"}
    assert oct(os.stat(tmp_path / "session-pins.json").st_mode & 0o777) == "0o600"

    store.set_pinned("claude", "claude-1", False)
    assert SessionPinStore(tmp_path).ids("claude") == set()


def test_session_pin_store_rejects_malformed_state(tmp_path):
    (tmp_path / "session-pins.json").write_text('{"claude":"bad"}')
    with pytest.raises(SessionPinStoreError, match="unreadable"):
        SessionPinStore(tmp_path)
