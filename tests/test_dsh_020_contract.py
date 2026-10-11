"""Regression tests for the DSH 0.2.0-rc.2 contract adaptation.

DSH 0.2.0-rc.2 changed four things this wrapper depends on.  Each test here
pins one of those facts, so the next DSH bump fails loudly instead of silently
degrading on a live phone:

* live assistant presentation is no longer the durable ``assistant/chunk``
  journal event; it is an opt-in process-local ``assistant-stream`` frame that
  carries no Session seq and must not advance the durable watermark.
* ``commands/execute`` renamed its attachment argument to
  ``submittedAttachments`` and validates the arg set exactly.
* the preset roster dropped its ``trust`` field.
* the adapted DSH release is reported as one host fact.
"""
from __future__ import annotations

import json

import httpx
import pytest

from cc_remote.wrapper.dsh_client import (
    DSH_ADAPTED_VERSION,
    DshClient,
    DshSessionHandle,
)
from cc_remote.wrapper.dsh_stream import (
    DshEventError,
    DshStreamTranslator,
)


async def _mocked_client(handler) -> DshClient:
    client = DshClient("http://127.0.0.1:3080")
    client._http = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    return client


class _StubMux:
    def __init__(self) -> None:
        self.opened: list[tuple[str, dict]] = []

    async def open(self, endpoint, payload, **callbacks):  # noqa: ANN001
        self.opened.append((endpoint, payload))
        return f"stream-{len(self.opened)}"


def _client_with_mux() -> tuple[DshClient, _StubMux]:
    client = DshClient("http://127.0.0.1:3080")
    mux = _StubMux()
    client._mux = mux
    client._mux_on_mux = lambda frame: None
    return client, mux


# ---- live assistant stream (replaces the removed assistant/chunk event) ----


@pytest.mark.asyncio
async def test_follow_request_opts_into_the_assistant_stream():
    client, mux = _client_with_mux()
    await client.follow_session("native-session")
    endpoint, payload = mux.opened[-1]
    assert endpoint == "session/follow"
    request = payload["args"]["request"]
    assert request["assistantStream"] is True
    assert request["address"] == {
        "kind": "session", "sessionId": "native-session",
    }


def test_stream_chunk_frames_reach_the_mux_surface_with_their_turn_and_step():
    client, _mux = _client_with_mux()
    frames: list[dict] = []
    client._adapt_follow_value({
        "type": "assistant-stream",
        "frame": {
            "type": "start", "attemptId": "attempt-1", "revision": 1,
            "startedAfterSeq": 7, "turn": 3, "step": 2,
        },
    }, "native-session", frames.append)
    assert frames == []

    client._adapt_follow_value({
        "type": "assistant-stream",
        "frame": {
            "type": "chunk", "attemptId": "attempt-1", "revision": 1,
            "index": 0, "time": 1_700_000_000_000,
            "chunk": {"type": "text-delta", "index": 0, "text": "hi"},
        },
    }, "native-session", frames.append)

    assert len(frames) == 1
    payload = frames[0]["payload"]
    assert payload["type"] == "session/assistant-stream"
    assert payload["sessionId"] == "native-session"
    assert (payload["turn"], payload["step"]) == (3, 2)
    assert payload["chunk"] == {"type": "text-delta", "index": 0, "text": "hi"}
    # Process-local frames carry no durable seq; the machine's watermark logic
    # must never see one.
    assert "seq" not in payload


def test_stream_end_forgets_the_attempt_so_late_chunks_are_dropped():
    client, _mux = _client_with_mux()
    frames: list[dict] = []
    for frame in (
        {"type": "start", "attemptId": "attempt-1", "turn": 3, "step": 2},
        {"type": "end", "attemptId": "attempt-1", "revision": 1,
         "outcome": {"kind": "committed", "eventType": "assistant/message",
                     "seq": 9}},
        {"type": "chunk", "attemptId": "attempt-1", "index": 0,
         "time": 1_700_000_000_000,
         "chunk": {"type": "text-delta", "index": 0, "text": "late"}},
    ):
        client._adapt_follow_value(
            {"type": "assistant-stream", "frame": frame},
            "native-session", frames.append,
        )
    assert frames == []


def test_stream_chunk_without_an_owning_attempt_is_dropped():
    client, _mux = _client_with_mux()
    frames: list[dict] = []
    client._adapt_follow_value({
        "type": "assistant-stream",
        "frame": {
            "type": "chunk", "attemptId": "unknown", "index": 0,
            "time": 1_700_000_000_000,
            "chunk": {"type": "text-delta", "index": 0, "text": "x"},
        },
    }, "native-session", frames.append)
    assert frames == []


def test_snapshot_baseline_seeds_the_attempt_for_mid_stream_attach():
    client, _mux = _client_with_mux()
    frames: list[dict] = []
    client._adapt_follow_value({
        "type": "snapshot", "cursor": 11,
        "projections": {},
        "assistantStream": {
            "revision": 2,
            "activeAttempt": {
                "attemptId": "attempt-9", "startedAfterSeq": 7,
                "turn": 5, "step": 1, "nextIndex": 0, "stream": [],
            },
        },
    }, "native-session", frames.append)
    frames.clear()

    client._adapt_follow_value({
        "type": "assistant-stream",
        "frame": {
            "type": "chunk", "attemptId": "attempt-9", "index": 0,
            "time": 1_700_000_000_000,
            "chunk": {"type": "text-delta", "index": 0, "text": "resumed"},
        },
    }, "native-session", frames.append)
    assert (frames[0]["payload"]["turn"], frames[0]["payload"]["step"]) == (5, 1)


def test_translator_stream_chunk_matches_the_removed_event_form():
    """The process-local path renders exactly what the old event did."""
    def run(chunk: dict, *, via: str):
        translator = DshStreamTranslator()
        translator.feed({
            "type": "turn/start", "seq": 1, "time": 1_700_000_000_001,
            "data": {"turn": 0},
        })
        if via == "event":
            return translator.feed({
                "type": "assistant/chunk", "seq": 2,
                "time": 1_700_000_000_002,
                "data": {"turn": 0, "step": 0, "chunk": chunk},
            })
        return translator.feed_stream_chunk(0, 0, chunk, 1_700_000_000_002)

    for chunk in (
        {"type": "block-start", "index": 0, "blockType": "text"},
        {"type": "text-delta", "index": 0, "text": "hello"},
        {"type": "reasoning-delta", "index": 1, "text": "why"},
    ):
        from_event = run(chunk, via="event")
        from_stream = run(chunk, via="stream")
        assert [row.type for row in from_event] == [
            row.type for row in from_stream
        ]
        assert len(from_event) == len(from_stream)


def test_translator_stream_chunk_rejects_a_malformed_chunk_or_timestamp():
    translator = DshStreamTranslator()
    translator.feed({
        "type": "turn/start", "seq": 1, "time": 1_700_000_000_001,
        "data": {"turn": 0},
    })
    with pytest.raises(DshEventError):
        translator.feed_stream_chunk(0, 0, "not-a-chunk", 1_700_000_000_002)
    with pytest.raises(DshEventError):
        translator.feed_stream_chunk(0, 0, {"type": "text-delta"}, None)


# ---- preset roster lost its trust field ----


@pytest.mark.asyncio
async def test_probe_projects_presets_that_no_longer_publish_trust():
    values = {
        "session/list": {"items": [{"sessionId": "s1", "cwd": "/tmp"}]},
        "session/modelCatalog": {"default": {
            "provider": "deepseek-official", "model": "deepseek-flash",
        }},
        # Exactly what DSH 0.2.0-rc.2 returns: id/order/isDefault, no trust.
        "agentPresets/list": {"presets": [
            {"id": "standard", "order": 1, "isDefault": True},
            {"id": "ptc", "order": 2, "isDefault": False},
        ]},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {"ok": True, "value": values[body["method"]]},
        })

    client = await _mocked_client(handler)
    try:
        result = await client.probe()
    finally:
        await client.stop()

    assert [row["id"] for row in result.presets] == ["standard", "ptc"]
    assert all(row["trust"] == "system" for row in result.presets)
    assert result.default_preset_id == "standard"


@pytest.mark.asyncio
async def test_probe_keeps_an_explicit_trust_when_dsh_publishes_one():
    values = {
        "session/list": {"items": [{"sessionId": "s1", "cwd": "/tmp"}]},
        "session/modelCatalog": {"default": {
            "provider": "deepseek-official", "model": "deepseek-flash",
        }},
        "agentPresets/list": {"presets": [
            {"id": "mine", "trust": "user", "isDefault": True},
        ]},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {"ok": True, "value": values[body["method"]]},
        })

    client = await _mocked_client(handler)
    try:
        result = await client.probe()
    finally:
        await client.stop()
    assert result.presets[0]["trust"] == "user"


@pytest.mark.asyncio
async def test_probe_reports_the_adapted_dsh_release():
    values = {
        "session/list": {"items": [{"sessionId": "s1", "cwd": "/tmp"}]},
        "session/modelCatalog": {"default": {
            "provider": "deepseek-official", "model": "deepseek-flash",
        }},
        "agentPresets/list": {"presets": []},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {"ok": True, "value": values[body["method"]]},
        })

    client = await _mocked_client(handler)
    try:
        result = await client.probe()
    finally:
        await client.stop()
    assert result.host["version"] == DSH_ADAPTED_VERSION == "0.2.0-rc.2"


# ---- commands/execute argument rename ----


@pytest.mark.asyncio
async def test_execute_command_sends_submitted_attachments():
    """DSH 0.2.0 rejects the old ``images`` key before the handler runs."""
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {"ok": True, "value": {
                "commandId": "command-1",
                "result": {"kind": "success"},
            }},
        })

    client = await _mocked_client(handler)
    handle = DshSessionHandle(client, "native-session", "/tmp")
    try:
        await handle.execute_command("/compact", rpc_id="rpc-1")
    finally:
        await client.stop()

    args = seen[-1]["payload"]["args"]
    assert args["submittedAttachments"] == []
    assert "images" not in args


def test_process_local_stream_frames_are_not_durable_events():
    """Guard the invariant behind the watermark: no seq, no event type."""
    client, _mux = _client_with_mux()
    frames: list[dict] = []
    client._adapt_follow_value({
        "type": "assistant-stream",
        "frame": {"type": "start", "attemptId": "a", "turn": 1, "step": 1},
    }, "native-session", frames.append)
    client._adapt_follow_value({
        "type": "assistant-stream",
        "frame": {"type": "chunk", "attemptId": "a", "index": 0,
                  "time": 1_700_000_000_000,
                  "chunk": {"type": "text-delta", "index": 0, "text": "x"}},
    }, "native-session", frames.append)
    assert all(
        frame["payload"]["type"] != "session/event" for frame in frames
    )


# ---- durable journal events that must never reach the wire ----


def test_dropped_events_are_known_in_both_live_and_history_mode():
    """Every one of these is a *required* 0.2.0 event; knowing them is what
    keeps one arriving event from failing an entire live turn or history page.
    """
    from cc_remote.wrapper.dsh_stream import _NON_PROJECTED_EVENTS

    # The sensitive one must be in the set: it carries the full system prompt.
    assert "system/message" in _NON_PROJECTED_EVENTS
    expected = {
        "assistant/attempt", "deliverables/presented", "developer/message",
        "feedback/message-delete", "feedback/message-put", "image/offload",
        "session-log-deepseek/delivery-accepted", "system/message",
        "workspace/changes",
    }
    assert _NON_PROJECTED_EVENTS == frozenset(expected)

    for strict in (True, False):
        for event_type in sorted(expected):
            translator = DshStreamTranslator(strict_history=strict)
            rows = translator.feed({
                "type": event_type, "seq": 3,
                "time": 1_700_000_000_000, "data": {},
            })
            assert rows == [], f"{event_type} must not project a row"


def test_system_prompt_text_never_appears_in_a_projected_row():
    translator = DshStreamTranslator()
    rows = translator.feed({
        "type": "system/message", "seq": 7, "time": 1_700_000_000_000,
        "surfaceOp": "append",
        "data": {
            "turn": 1, "step": 1,
            "message": {
                "role": "system",
                "content": [{"type": "text", "text": "SECRET WORKSPACE RULES"}],
            },
        },
    })
    assert rows == []


def test_renamed_ptc_dispatch_still_projects_a_process_row():
    for event_type in ("tool/ptc-dispatch-start", "tool/ptc-dispatch"):
        translator = DshStreamTranslator()
        rows = translator.feed({
            "type": event_type, "seq": 4, "time": 1_700_000_000_000,
            "data": {"name": "run_code", "arguments": {"secret": "x"}},
        })
        assert [row.type for row in rows] == ["process"]
        # Payloads are never proxied through the relay.
        assert "secret" not in str(getattr(rows[0], "summary", "") or "")
        assert rows[0].title == event_type


def test_subagent_and_team_lifecycle_metadata_projects_rows_without_payload():
    for event_type in (
        "subagent/catalog", "subagent/model-selection-policy",
        "team/member", "team/task", "team/message/queued",
    ):
        translator = DshStreamTranslator()
        rows = translator.feed({
            "type": event_type, "seq": 5, "time": 1_700_000_000_000,
            "data": {"label": "child", "content": [{"text": "secret"}]},
        })
        assert [row.type for row in rows] == ["process"]
        assert rows[0].item_id
        assert "secret" not in str(getattr(rows[0], "summary", ""))


# ---- subagent Session addressing (DSH 0.2.0) ----


def test_address_helper_prefers_the_parented_form_for_a_subagent_row():
    from cc_remote.wrapper.dsh_client import dsh_address_from_row

    assert dsh_address_from_row({
        "sessionId": "plain",
    }) == {"kind": "session", "sessionId": "plain"}

    assert dsh_address_from_row({
        "sessionId": "child",
        "origin": "subagent",
        "parentSessionId": "parent",
        "projections": {"values": {"subagent": {"mode": "continuable"}}},
    }) == {
        "kind": "subagent",
        "parentSessionId": "parent",
        "childSessionId": "child",
        "mode": "continuable",
    }


def test_address_helper_degrades_to_unknown_mode_and_refuses_no_parent():
    from cc_remote.wrapper.dsh_client import dsh_address_from_row

    address = dsh_address_from_row({
        "sessionId": "child", "origin": "subagent",
        "parentSessionId": "parent", "projections": {},
    })
    assert address is not None and address["mode"] == "unknown"

    # A subagent with no durable parent cannot be addressed; refuse rather than
    # fall back to a form DSH will reject.
    assert dsh_address_from_row({
        "sessionId": "child", "origin": "subagent",
    }) is None


def test_cursor_hint_reads_the_roster_projection():
    from cc_remote.wrapper.dsh_client import dsh_cursor_hint_from_row

    assert dsh_cursor_hint_from_row(
        {"projections": {"asOfSeq": 710}}
    ) == 710
    assert dsh_cursor_hint_from_row({"projections": {}}) is None
    assert dsh_cursor_hint_from_row({}) is None
    assert dsh_cursor_hint_from_row(
        {"projections": {"asOfSeq": True}}
    ) is None


@pytest.mark.asyncio
async def test_history_page_uses_the_cached_subagent_address():
    from cc_remote.wrapper.dsh_history import DshHistory

    class AddressAwareClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def session_address(self, native_sid: str) -> dict:
            return {
                "kind": "subagent", "parentSessionId": "parent",
                "childSessionId": native_sid, "mode": "continuable",
            }

        async def call(self, method: str, payload: dict):
            self.calls.append((method, payload))
            return {
                "records": [], "hasMore": False,
                "projections": {"asOfSeq": 0, "values": {}},
            }

    client = AddressAwareClient()
    history = DshHistory(client)
    await history.page(
        "native-child", before=None, limit=4, through_seq=999,
    )
    method, payload = client.calls[-1]
    assert method == "session.history"
    assert payload["address"]["kind"] == "subagent"
    assert payload["address"]["parentSessionId"] == "parent"
    assert payload["throughSeq"] == 999


@pytest.mark.asyncio
async def test_a_lagging_roster_watermark_never_truncates_the_newest_page():
    """``projections.asOfSeq`` is a projection watermark, not the page cursor.

    DSH requires ``throughSeq`` to be the follow opening frame's log cut.  A
    roster watermark lags real events, so folding it into the request (as an
    earlier revision did) silently hides the newest messages: the page comes
    back bounded before them.
    """
    from cc_remote.wrapper.dsh_history import DshHistory

    class LaggingHintClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def session_address(self, native_sid: str) -> dict:
            return {"kind": "session", "sessionId": native_sid}

        def session_cursor_hint(self, _native_sid: str) -> int | None:
            # Far behind the live cursor the caller passes in.
            return 100

        async def call(self, method: str, payload: dict):
            self.calls.append((method, payload))
            return {
                "records": [], "hasMore": False,
                "projections": {"asOfSeq": 0, "values": {}},
            }

    client = LaggingHintClient()
    history = DshHistory(client)
    await history.page(
        "native-session", before=None, limit=8, through_seq=900,
    )
    assert client.calls[-1][1]["throughSeq"] == 900, (
        "the follow opening cursor must reach DSH unchanged"
    )


# ---- 0.2.0 projections: permissions lost its options, modelSelection is new ----


def test_permissions_projection_is_selection_only():
    """DSH 0.2.0's permissions projection carries only currentValue.

    The 0.1.2 shape required an `options` list, so every 0.2.0 projection was
    rejected as invalid and the client sat on its loading placeholder forever.
    """
    from cc_remote.wrapper.dsh_client import DshClient, DshSessionHandle

    handle = DshSessionHandle(DshClient("http://127.0.0.1:3080"), "s", "/tmp")
    assert handle.apply_projection(
        "permissions", {"currentValue": "danger-full-access"}, 4,
    ) is True
    assert handle.permission_mode == "danger-full-access"
    # The projection never carries options now, and none are invented.
    assert handle.permission_options == ()


def test_permission_catalog_supplies_the_selectable_options():
    from cc_remote.wrapper.dsh_client import DshClient, DshSessionHandle

    handle = DshSessionHandle(DshClient("http://127.0.0.1:3080"), "s", "/tmp")
    assert handle.apply_permission_catalog({"options": [
        {"value": "read-only", "name": "Read Only"},
        {"value": "danger-full-access", "name": "danger-full-access"},
        {"value": "custom", "name": "Custom"},
    ]}) is True
    assert [row["id"] for row in handle.permission_options] == [
        "read-only", "danger-full-access",
    ]
    assert handle.permission_options[-1]["name"] == "Full Access"
    assert handle.permission_options[-1]["danger"] is True


def test_permission_catalog_is_allowlisted_and_sends_empty_args():
    from cc_remote.wrapper.dsh_client import DshClient

    client = DshClient("http://127.0.0.1:3080")
    assert "permissionPresets/catalog" in client.ALLOWED_METHODS
    # Declares no parameters, so it must not be wrapped in a "request" field.
    assert client._map_012_call("permissionPresets/catalog", {}) == (
        "permissionPresets/catalog", None, {},
    )


def test_model_selection_projection_supplies_model_and_effort():
    """The per-session route and strength, which modelCatalog no longer reports."""
    from cc_remote.wrapper.dsh_client import DshClient, DshSessionHandle

    handle = DshSessionHandle(DshClient("http://127.0.0.1:3080"), "s", "/tmp")
    assert handle.apply_projection("modelSelection", {
        "lastUsed": {
            "provider": "deepseek-official", "model": "deepseek-flash",
            "reasoningEffort": "low",
        },
        "next": {
            "provider": "deepseek-official", "model": "deepseek-flash",
            "reasoningEffort": "high",
        },
    }, 9) is True
    assert handle.model == "dsh://deepseek-official/deepseek-flash"
    assert handle.effort == "high", "next is the selection the following turn uses"

    with pytest.raises(Exception):
        handle.apply_projection("modelSelection", {"next": {}}, 10)
