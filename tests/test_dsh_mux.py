"""Tests for the remote.mux logical-stream client."""
from __future__ import annotations

import asyncio
import json

import pytest

from cc_remote.wrapper.dsh_mux import (
    MuxStreamError,
    RemoteMuxClient,
)


class FakeWebSocket:
    """Minimal in-memory websocket double for RemoteMuxClient tests."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.inbound: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self):
        return await self.inbound.get()

    async def close(self) -> None:
        self.closed = True


async def _client_with_ws() -> tuple[RemoteMuxClient, FakeWebSocket]:
    client = RemoteMuxClient("ws://127.0.0.1:3080/api/remote.mux", "cookie=1")
    fake = FakeWebSocket()
    client._ws = fake
    client._closed = False
    return client, fake


@pytest.mark.asyncio
async def test_open_sends_open_frame_and_registers_stream():
    client, fake = await _client_with_ws()
    stream = await client.open("$events", {"args": {}})
    assert fake.sent[-1] == {
        "type": "open",
        "streamId": stream,
        "endpoint": "$events",
        "payload": {"args": {}},
    }
    assert stream in client._streams


@pytest.mark.asyncio
async def test_dispatch_routes_item_and_end():
    client, fake = await _client_with_ws()
    values: list[dict] = []
    ended = asyncio.Event()
    stream = await client.open(
        "session/follow",
        {"args": {"request": {}}},
        on_value=lambda v: values.append(v),
        on_end=lambda: ended.set(),
    )
    await client._dispatch({
        "type": "item", "streamId": stream,
        "value": {"type": "event", "event": {"type": "turn/start"}},
    })
    assert values == [{"type": "event", "event": {"type": "turn/start"}}]
    await client._dispatch({"type": "end", "streamId": stream})
    assert ended.is_set()
    # After end the stream entry is gone.
    assert stream not in client._streams


@pytest.mark.asyncio
async def test_dispatch_error_invokes_on_error_and_removes_stream():
    client, fake = await _client_with_ws()
    errors: list[MuxStreamError] = []
    stream = await client.open(
        "$events", {"args": {}}, on_error=lambda e: errors.append(e)
    )
    await client._dispatch({
        "type": "error", "streamId": stream,
        "error": {"code": "gateway/bad-request", "message": "nope"},
    })
    assert len(errors) == 1
    assert errors[0].code == "gateway/bad-request"
    assert "nope" in str(errors[0])
    assert stream not in client._streams


@pytest.mark.asyncio
async def test_cancel_removes_stream_and_sends_cancel_frame():
    client, fake = await _client_with_ws()
    stream = await client.open("session/follow", {"args": {}})
    await client.cancel(stream)
    assert fake.sent[-1] == {"type": "cancel", "streamId": stream}
    assert stream not in client._streams


@pytest.mark.asyncio
async def test_close_cancels_reader_and_clears_state():
    client, fake = await _client_with_ws()
    stream = await client.open("$events", {"args": {}})
    # No reader task in tests, but close must still be safe.
    await client.close()
    assert client._ws is None
    assert stream not in client._streams
    assert client._closed is True
