"""DSH 0.1.2 remote.mux event-stream client.

DSH 0.1.2 replaced the two bare downstream WebSockets (``/api/events.mux`` and
``/api/events.host``) with one multiplexed ``/api/remote.mux`` socket carrying
any number of Typert Remote *logical streams*.  This module owns the physical
connection and the open/cancel/item/end/error wire protocol; consumers open
streams by endpoint (``$events``, ``session/control``, ``session/follow``,
``workspace/follow``) and receive decoded ``value`` frames via a callback.

Wire protocol (mirrors @deepseek-ai/dsh-api-gateway):
    client -> server: {"type":"open","streamId","endpoint","payload"}
                      {"type":"cancel","streamId"}
    server -> client: {"type":"item","streamId","value"}
                      {"type":"end","streamId"}
                      {"type":"error","streamId","error":{code,message,details}}
The server also sends WebSocket-level Ping frames; ``websockets`` answers
Pong automatically.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import connect

from cc_remote.log import logger

log = logger("cc_remote.wrapper.dsh_mux")

FrameCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class MuxError(RuntimeError):
    """Base class for a remote.mux transport or protocol failure."""


class MuxStreamError(MuxError):
    """A logical stream failed; carries the server-side error payload."""

    def __init__(self, code: str, message: str, details: object = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.details = details


class RemoteMuxClient:
    """One authenticated remote.mux connection with logical-stream demux.

    Usage::

        mux = RemoteMuxClient(url, cookie, max_frame_bytes=...)
        await mux.connect()
        stream_id = await mux.open("$events", {}, on_value=on_events_value)
        ...
        await mux.cancel(stream_id)
        await mux.close()
    """

    def __init__(
        self,
        url: str,
        cookie: str | None,
        *,
        max_frame_bytes: int = 32 * 1024 * 1024,
        open_timeout: float = 10.0,
    ) -> None:
        self.url = url
        self.cookie = cookie
        self.max_frame_bytes = max(1024, int(max_frame_bytes))
        self.open_timeout = float(open_timeout)
        self._ws = None
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        # streamId -> {on_value, on_end, on_error}
        self._streams: dict[str, dict[str, Any]] = {}
        self._send_lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._ws is not None:
            return
        headers = {}
        if self.cookie:
            headers["cookie"] = self.cookie
        self._ws = await connect(
            self.url,
            additional_headers=headers or None,
            max_size=self.max_frame_bytes,
            max_queue=64,
            open_timeout=self.open_timeout,
            proxy=None,
            # DSH's gateway owns connection health: it pings every 2s and
            # terminates a socket after two missed pongs, so the WebSocket
            # client must NOT run its own keepalive.  The library's default
            # 20s ping/pong timeout drops a healthy carrier whenever DSH's event
            # loop is briefly busy (long tool batches, event floods), which
            # tears down every session follow stream with it.
            ping_interval=None,
        )
        self._closed = False
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="cc-remote-dsh-mux-reader"
        )

    async def _read_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    log.warning("remote.mux emitted binary frame; dropped")
                    continue
                if len(raw.encode("utf-8")) > self.max_frame_bytes:
                    log.warning("remote.mux frame exceeds limit; dropped")
                    continue
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    log.warning("remote.mux emitted invalid JSON; dropped")
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                log.warning("remote.mux read loop ended", error=str(exc))

    async def _dispatch(self, message: dict[str, Any]) -> None:
        frame_type = message.get("type")
        stream_id = message.get("streamId")
        if frame_type == "item" and isinstance(stream_id, str):
            entry = self._streams.get(stream_id)
            if entry is not None:
                value = message.get("value")
                if isinstance(value, dict):
                    await self._invoke(entry.get("on_value"), value)
            return
        if frame_type == "end" and isinstance(stream_id, str):
            entry = self._streams.pop(stream_id, None)
            if entry is not None:
                await self._invoke(entry.get("on_end"))
            return
        if frame_type == "error" and isinstance(stream_id, str):
            entry = self._streams.pop(stream_id, None)
            if entry is not None:
                error = message.get("error")
                code = error.get("code") if isinstance(error, dict) else "stream/error"
                detail = error.get("message") if isinstance(error, dict) else ""
                await self._invoke(entry.get("on_error"), MuxStreamError(code, detail, error))
            return
        # Frames without a recognized stream (e.g. ping handling) are ignored.
        log.debug("remote.mux unmatched frame", type=frame_type, stream_id=stream_id)

    async def open(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        on_value: FrameCallback | None = None,
        on_end: FrameCallback | None = None,
        on_error: FrameCallback | None = None,
    ) -> str:
        if self._ws is None:
            raise MuxError("remote.mux is not connected")
        stream_id = f"dsh-{uuid4().hex[:10]}"
        self._streams[stream_id] = {
            "on_value": on_value,
            "on_end": on_end,
            "on_error": on_error,
        }
        frame = {
            "type": "open",
            "streamId": stream_id,
            "endpoint": endpoint,
            "payload": payload,
        }
        await self._send(frame)
        return stream_id

    async def cancel(self, stream_id: str) -> None:
        self._streams.pop(stream_id, None)
        if self._ws is not None:
            await self._send({"type": "cancel", "streamId": stream_id})

    async def _send(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise MuxError("remote.mux is not connected")
        raw = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        async with self._send_lock:
            await ws.send(raw)

    async def close(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        self._streams.clear()

    async def wait_closed(self) -> None:
        """Await the reader loop's natural end (socket close or fatal read)."""
        task = self._reader_task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    @staticmethod
    async def _invoke(
        callback: Callable[..., Any] | None, *args: Any
    ) -> None:
        if callback is None:
            return
        result = callback(*args)
        if asyncio.iscoroutine(result):
            await result
