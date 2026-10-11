from __future__ import annotations

import base64

import pytest

from cc_remote.wrapper.dsh_client import DshProtocolError
from cc_remote.wrapper.dsh_history import DshHistory


def event(seq: int, kind: str, data: dict, **extra) -> dict:
    return {
        "event": {
            "type": kind,
            "seq": seq,
            "time": 1_700_000_000_000 + seq,
            "data": data,
            **extra,
        }
    }


class FakeClient:
    def __init__(self, history: dict, attachment: dict | None = None) -> None:
        self.history = history
        self.attachment = attachment
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, payload: dict):
        self.calls.append((method, payload))
        if method == "session.history":
            # DSH 0.1.2 session/page returns {records: [{type:"event", event}]};
            # the fixtures below carry the legacy {events: [{event}]} shape, so
            # adapt on the way in to keep the assertions focused on translation.
            events = self.history.get("events")
            if events is not None:
                page: dict = {
                    "records": [
                        {"type": "event", **entry} for entry in events
                    ],
                    "hasMore": self.history.get("hasMore", False),
                }
                if "projections" in self.history:
                    page["projections"] = self.history["projections"]
                return page
            return self.history
        if method == "session.attachment":
            return self.attachment
        raise AssertionError(method)

    async def fetch_session_cursor(self, _native_sid: str) -> int | None:
        return 10_000


@pytest.mark.asyncio
async def test_session_configuration_preamble_does_not_create_phantom_turn():
    client = FakeClient({
        "events": [
            event(0, "permission/preset", {"preset": "coding-agent"}),
            event(1, "sandbox/mode", {"mode": "workspace-write"}),
            event(2, "approval/policy", {"policy": "on-request"}),
            event(4, "turn/start", {"turn": 0}),
            event(7, "user/message", {
                "source": {"kind": "user", "rpcId": "rpc-a"},
                "content": [{"type": "text", "text": "hello"}],
            }, surfaceOp="append"),
            event(8, "assistant/message", {
                "turn": 0,
                "step": 0,
                "message": {
                    "content": [{"type": "text", "text": "world"}],
                },
            }, surfaceOp="append"),
            event(9, "turn/end", {
                "turn": 0,
                "reason": {"kind": "completed"},
            }),
        ],
        "hasMore": False,
    })
    history = DshHistory(client)  # type: ignore[arg-type]

    page = await history.page("session-a", before=None, limit=4)

    assert [turn.id for turn in page.turns] == ["dsh-msg-7"]
    assert page.turns[0].done is True
    assert page.turns[0].forkPointId == "dsh-seq-9"
    assert not any(
        row.get("type") == "process"
        and str(row.get("item_id", "")).startswith("dsh-event-")
        for row in page.events
    )


@pytest.mark.asyncio
async def test_plugin_context_before_human_does_not_create_phantom_turn():
    client = FakeClient({
        "events": [
            event(444, "turn/start", {"turn": 2}),
            event(448, "user/message", {
                "id": "approval-context",
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": "The approval policy changed from ask to never.",
                }],
                "source": {"kind": "plugin", "plugin": "user-approval"},
            }, surfaceOp="append"),
            event(449, "user/message", {
                "id": "human-message",
                "role": "user",
                "content": [{"type": "text", "text": "install libreoffice"}],
                "source": {"kind": "user", "rpcId": "remote-message"},
            }, surfaceOp="append"),
            event(450, "user/message", {
                "id": "runtime-context",
                "role": "user",
                "content": [{"type": "text", "text": "runtime context"}],
                "source": {
                    "kind": "plugin",
                    "plugin": "@deepseek-ai/dsh-system-prompt",
                },
            }, surfaceOp="append"),
            event(1109, "assistant/message", {
                "turn": 2,
                "step": 2,
                "message": {
                    "content": [{"type": "text", "text": "installed"}],
                },
            }, surfaceOp="append"),
            event(1111, "turn/end", {
                "turn": 2,
                "reason": {"kind": "completed"},
            }),
        ],
        "hasMore": False,
    })
    history = DshHistory(client)  # type: ignore[arg-type]

    page = await history.page("session-a", before=None, limit=4)

    assert [turn.id for turn in page.turns] == ["dsh-msg-449"]
    assert page.turns[0].done is True
    assert page.turns[0].forkPointId == "dsh-seq-1111"
    detail = history.detail("session-a", "dsh-msg-449")
    assert detail is not None
    assert [
        row["item_id"] for row in detail if row["type"] == "process"
    ][:2] == ["dsh-context-448", "dsh-context-450"]


@pytest.mark.asyncio
async def test_detail_cache_does_not_shift_after_metadata_only_group():
    client = FakeClient({
        "events": [
            event(1, "request/header", {
                "header": {"config": {"provider": "deepseek", "model": "v4"}},
            }),
            event(2, "turn/start", {"turn": 0}),
            event(3, "user/message", {
                "source": {"kind": "user", "rpcId": "rpc-a"},
                "content": [{"type": "text", "text": "hello"}],
            }, surfaceOp="append"),
            event(4, "assistant/message", {
                "turn": 0,
                "step": 0,
                "message": {
                    "content": [{"type": "text", "text": "world"}],
                },
            }, surfaceOp="append"),
            event(5, "turn/end", {
                "turn": 0,
                "reason": {"kind": "completed"},
            }),
        ],
        "hasMore": False,
    })
    history = DshHistory(client)  # type: ignore[arg-type]

    page = await history.page("session-a", before=None, limit=4)

    assert [turn.id for turn in page.turns] == ["dsh-msg-3"]
    detail = history.detail("session-a", "dsh-msg-3")
    assert detail is not None
    assert any(row["type"] == "user_msg" for row in detail)
    assert not any(row["type"] == "model" for row in detail)


@pytest.mark.asyncio
async def test_history_image_is_session_and_turn_scoped():
    raw = b"tiny-image"
    client = FakeClient({
        "events": [
            event(10, "user/message", {
                "source": {"kind": "user"},
                "content": [{
                    "type": "image",
                    "attachment": {
                        "attachmentId": "attachment-a",
                        "mediaType": "image/png",
                        "width": 2,
                        "height": 3,
                        "bytes": len(raw),
                    },
                }],
            }, surfaceOp="append"),
        ],
        "hasMore": False,
    }, {
        "attachment": {
            "attachmentId": "attachment-a",
            "mediaType": "image/png",
            "width": 2,
            "height": 3,
            "bytes": len(raw),
        },
        "data": base64.b64encode(raw).decode("ascii"),
    })
    history = DshHistory(client)  # type: ignore[arg-type]

    page = await history.page("session-a", before=None, limit=4)
    image_id = page.turns[0].imageRefs[0]["image_id"]
    image = await history.image("session-a", "dsh-msg-10", image_id)

    assert image is not None
    assert image.data == base64.b64encode(raw).decode("ascii")
    assert await history.image("session-b", "dsh-msg-10", image_id) is None


@pytest.mark.asyncio
async def test_history_materializes_steers_as_distinct_nonforkable_segments():
    raw = b"steered-image"
    steer_message = {
        "id": "native-steer",
        "role": "user",
        "source": {"kind": "user", "rpcId": "remote-steer"},
        "content": [
            {"type": "text", "text": "change direction"},
            {
                "type": "image",
                "attachment": {
                    "attachmentId": "attachment-steer",
                    "mediaType": "image/png",
                    "width": 4,
                    "height": 5,
                    "bytes": len(raw),
                },
            },
        ],
    }
    client = FakeClient({
        "events": [
            event(1, "turn/start", {"turn": 7}),
            event(2, "user/message", {
                "source": {"kind": "user", "rpcId": "remote-first"},
                "content": [{"type": "text", "text": "first"}],
            }, surfaceOp="append"),
            event(3, "agent/inbox/spliced", {
                "target": "next-step",
                "start": 0,
                "inserted": [steer_message],
            }),
            event(4, "agent/inbox/spliced", {
                "target": "next-step",
                "start": 0,
                "removedCount": 1,
                "inserted": [],
            }),
            event(5, "user/message", steer_message, surfaceOp="append"),
            event(6, "assistant/message", {
                "turn": 7,
                "step": 1,
                "message": {
                    "content": [{"type": "text", "text": "done"}],
                },
            }, surfaceOp="append"),
            event(7, "turn/end", {
                "turn": 7,
                "reason": {"kind": "completed"},
            }),
        ],
        "hasMore": False,
    }, {
        "attachment": {
            "attachmentId": "attachment-steer",
            "mediaType": "image/png",
            "width": 4,
            "height": 5,
            "bytes": len(raw),
        },
        "data": base64.b64encode(raw).decode("ascii"),
    })
    history = DshHistory(client)  # type: ignore[arg-type]

    page = await history.page("session-a", before=None, limit=4)

    assert [turn.id for turn in page.turns] == ["dsh-msg-2", "dsh-msg-5"]
    assert page.turns[0].done is True
    assert page.turns[0].forkPointId is None
    assert page.turns[1].done is True
    assert page.turns[1].forkPointId == "dsh-seq-7"
    assert page.turns[1].clientMsgId == "remote-steer"
    image_id = page.turns[1].imageRefs[0]["image_id"]
    assert await history.image(
        "session-a", "remote-steer", image_id,
    ) is not None
    assert any(
        row.get("type") == "turn_steered"
        for row in history.detail("session-a", "dsh-msg-5") or ()
    )


@pytest.mark.asyncio
async def test_history_materializes_command_with_private_client_alias():
    client = FakeClient({
        "events": [
            event(30, "command/run", {
                "commandId": "command-1",
                "name": "compact",
                "args": " now",
                "source": {"kind": "user"},
            }),
            event(31, "command/done", {
                "commandId": "command-1",
                "kind": "success",
                "text": "Compacted",
            }),
        ],
        "hasMore": False,
    })
    history = DshHistory(client)  # type: ignore[arg-type]

    page = await history.page(
        "session-a",
        before=None,
        limit=4,
        command_aliases={"command-1": "remote-command"},
    )

    assert len(page.turns) == 1
    turn = page.turns[0]
    assert turn.id == "dsh-msg-30"
    assert turn.clientMsgId == "remote-command"
    assert turn.prompt == "/compact now"
    assert turn.done is True
    assert turn.forkPointId is None
    assert [row["type"] for row in page.events] == [
        "user_msg", "process", "process", "turn_end",
    ]


@pytest.mark.asyncio
async def test_page_uses_turn_cursor_and_marks_local_truncation_has_more():
    rows = []
    for index in range(3):
        seq = 20 + index * 3
        rows.extend([
            event(seq, "turn/start", {"turn": index}),
            event(seq + 1, "user/message", {
                "source": {"kind": "user"},
                "content": [{"type": "text", "text": f"q{index}"}],
            }, surfaceOp="append"),
            event(seq + 2, "turn/end", {
                "turn": index,
                "reason": {"kind": "completed"},
            }),
        ])
    client = FakeClient({"events": rows, "hasMore": False})
    history = DshHistory(client)  # type: ignore[arg-type]

    page = await history.page("session-a", before="dsh-msg-21", limit=2)

    # 0.1.2 maps the legacy before cursor (seq 21) to throughSeq=20.
    assert client.calls[0][1]["throughSeq"] == 20
    assert [turn.id for turn in page.turns] == ["dsh-msg-24", "dsh-msg-27"]
    assert page.has_more is True


@pytest.mark.asyncio
async def test_history_rejects_an_event_count_beyond_the_cpu_bound():
    history = DshHistory(FakeClient({
        "events": [
            {"event": {"type": "turn/start", "seq": i, "time": 0, "data": {}}}
            for i in range(4)
        ],
        "hasMore": False,
    }))  # type: ignore[arg-type]
    history.EVENTS_PER_PAGE_MAX = 3

    with pytest.raises(DshProtocolError, match="event limit"):
        await history.page("session-a", before=None, limit=4)


@pytest.mark.asyncio
async def test_history_rejects_an_unbounded_projection_sequence():
    history = DshHistory(FakeClient({
        "events": [],
        "hasMore": False,
        "projections": {
            "asOfSeq": 9_007_199_254_740_992,
            "values": {},
        },
    }))  # type: ignore[arg-type]

    with pytest.raises(DshProtocolError, match="invalid projections"):
        await history.page("session-a", before=None, limit=4)


def test_detail_cache_is_bounded_by_entries_and_bytes():
    history = DshHistory(FakeClient({}))  # type: ignore[arg-type]
    history.DETAIL_CACHE_ENTRIES = 2
    history.DETAIL_CACHE_BYTES = 1024

    history._remember_detail("session-a", "turn-1", ({"text": "one"},))
    history._remember_detail("session-a", "turn-2", ({"text": "two"},))
    history._remember_detail("session-a", "turn-3", ({"text": "three"},))

    assert history.detail("session-a", "turn-1") is None
    assert history.detail("session-a", "turn-2") is not None
    assert history.detail("session-a", "turn-3") is not None
    assert history._detail_cache_bytes <= history.DETAIL_CACHE_BYTES

    oversized = DshHistory(FakeClient({}))  # type: ignore[arg-type]
    oversized.DETAIL_CACHE_BYTES = 8
    oversized._remember_detail(
        "session-a", "oversized", ({"text": "too large"},)
    )
    assert oversized.detail("session-a", "oversized") is None
    assert oversized._detail_cache_bytes == 0

    history.invalidate("session-a")
    assert history._detail_cache_bytes == 0


@pytest.mark.asyncio
async def test_tool_only_turn_cursor_pages_older_without_rescanning_itself():
    """A segment that opens with a tool call is named after that tool id.

    DSH 0.1.2 turn ids are not all sequence-bearing: the id below is
    ``dsh-step-<turn>-<step>``, which cannot be parsed into a sequence. It is
    still the cursor the client sends back for "load older", so it must resolve
    through the ids this process emitted, or every older page fails. This is the
    shape a live session produced when a page opened inside a running turn.
    """
    client = FakeClient({
        "events": [
            event(41, "tool/call", {
                "turn": 3, "step": 64, "callId": "call-64",
                "name": "Bash", "arguments": "{}",
            }, surfaceOp="append"),
            event(42, "tool/result", {
                "turn": 3, "step": 64,
                "message": {"source": {"callId": "call-64"}, "content": []},
            }, surfaceOp="append"),
        ],
        "hasMore": True,
    })
    history = DshHistory(client)  # type: ignore[arg-type]

    newest = await history.page("session-a", before=None, limit=4)
    assert newest.oldest_id == "dsh-step-3-64"
    # The private sequence key must never leak into emitted events or turns.
    assert all("_seq" not in turn.model_dump() for turn in newest.turns)
    assert all("_seq" not in row for row in newest.events)

    client.calls.clear()
    older = await history.page("session-a", before=newest.oldest_id, limit=4)
    assert client.calls[0][0] == "session.history"
    assert client.calls[0][1]["throughSeq"] == 40, (
        "the cursor must bound the window strictly older than its own turn"
    )
    assert isinstance(older, type(newest))


@pytest.mark.asyncio
async def test_unknown_history_cursor_fails_loudly():
    client = FakeClient({"events": [], "hasMore": False})
    history = DshHistory(client)  # type: ignore[arg-type]

    with pytest.raises(DshProtocolError):
        await history.page("session-a", before="dsh-from-另一个进程", limit=4)
    assert not client.calls, "an unresolvable cursor must not page a guess"


@pytest.mark.asyncio
async def test_sequence_cursor_still_pages_arithmetically():
    client = FakeClient({"events": [], "hasMore": False})
    history = DshHistory(client)  # type: ignore[arg-type]

    await history.page("session-a", before="dsh-msg-21", limit=2)
    assert client.calls[0][1]["throughSeq"] == 20


class _CursorCorrectingClient:
    """A client whose page cursor is rejected once with DSH's own bound."""

    def __init__(self, suggested: int) -> None:
        self.suggested = suggested
        self.calls: list[dict] = []

    async def call(self, method: str, payload: dict):
        self.calls.append(dict(payload))
        if len(self.calls) == 1:
            from cc_remote.wrapper.dsh_client import DshRpcError
            raise DshRpcError(
                "gateway/internal",
                "session page through seq 1113554 is past cursor "
                f"{self.suggested}",
            )
        return {"records": [], "hasMore": False,
                "projections": {"asOfSeq": 0, "values": {}}}


@pytest.mark.asyncio
async def test_history_page_takes_dshs_own_bound_when_our_cursor_is_past_it():
    """DSH names the cursor it will accept; guessing one hides newest messages."""
    client = _CursorCorrectingClient(suggested=9025)
    await DshHistory(client).page(
        "native-session", before=None, limit=8, through_seq=1113554,
    )
    assert [call["throughSeq"] for call in client.calls] == [1113554, 9025]


@pytest.mark.asyncio
async def test_history_page_does_not_retry_an_unrelated_failure():
    from cc_remote.wrapper.dsh_client import DshRpcError

    class _UnrelatedFailureClient:
        def __init__(self) -> None:
            self.calls = 0

        async def call(self, method: str, payload: dict):
            self.calls += 1
            raise DshRpcError("gateway/internal", "some other refusal")

    client = _UnrelatedFailureClient()
    with pytest.raises(DshRpcError):
        await DshHistory(client).page(
            "native-session", before=None, limit=8, through_seq=10,
        )
    assert client.calls == 1, "only a past-cursor refusal may be retried"
