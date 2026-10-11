"""Authoritative queue recovery and editing pauses, without native model calls."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from cc_remote.protocol import GetQueuedQuery, Hello, Query, UpdateQueuedQuery, deserialize, serialize
from tests.test_multisession import _mk_ctx, _mk_machine


def setup_queue(engine):
    machine, transport = _mk_machine()
    ctx = _mk_ctx(f"{engine}@queue-edit")
    ctx.engine = engine
    machine.sessions[ctx.key] = ctx
    machine.focused_sid = ctx.key
    ctx.state = "idle"
    query = Query(sid=ctx.key, msg_id="queued-one", prompt="original", delivery="queue",
                  client_id="desktop", cmd_id="enqueue")
    ctx.queued_queries = [query]
    ctx.queued_query_bytes = machine._queued_query_bytes = machine._queued_query_size(query)
    machine._queued_query_count = 1
    return machine, transport, ctx


def detail(ctx, action, client="desktop"):
    return GetQueuedQuery(sid=ctx.key, msg_id="queued-one", client_id=client,
                          cmd_id=f"edit-{action}", edit_action=action)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["dsh", "codex"])
async def test_edit_holds_idle_queue_until_atomic_save(engine, monkeypatch):
    machine, _, ctx = setup_queue(engine)
    sent = []
    async def launch(_ctx, command):
        sent.append(command.prompt)
    monkeypatch.setattr(machine, "_handle_immediate_query", launch)
    result = await machine._handle_get_queued_query(detail(ctx, "begin"))
    assert result.prompt == "original"
    task = asyncio.create_task(machine._drain_query_queue(ctx))
    ctx.queued_query_drain_task = task
    try:
        await asyncio.sleep(.02)
        assert sent == [] and len(ctx.queued_queries) == 1
        # A second client cannot overwrite the editor or release its hold.
        conflict = await machine._handle_get_queued_query(detail(ctx, "begin", "phone"))
        assert conflict.prompt is None and "另一端" in conflict.error
        await machine._handle_get_queued_query(detail(ctx, "end", "phone"))
        denied = await machine._handle_update_queued_query(UpdateQueuedQuery(
            sid=ctx.key, msg_id="queued-one", prompt="wrong", cmd_id="wrong", client_id="phone"))
        assert not denied.updated and sent == []
        saved = await machine._handle_update_queued_query(UpdateQueuedQuery(
            sid=ctx.key, msg_id="queued-one", prompt="edited", cmd_id="save", client_id="desktop"))
        assert saved.updated
        await asyncio.wait_for(task, 1)
        assert sent == ["edited"] and ctx.queued_queries == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("end", [True, False])
async def test_closed_or_expired_editor_does_not_strand_queue(end, monkeypatch):
    machine, _, ctx = setup_queue("dsh")
    sent = []
    async def launch(_ctx, command):
        sent.append(command.prompt)
    monkeypatch.setattr(machine, "_handle_immediate_query", launch)
    await machine._handle_get_queued_query(detail(ctx, "begin"))
    if end:
        await machine._handle_get_queued_query(detail(ctx, "end"))
    else:
        ctx.queued_query_editors["queued-one"] = ("desktop", time.monotonic() - 1)
    await asyncio.wait_for(machine._drain_query_queue(ctx), 1)
    assert sent == ["original"]


@pytest.mark.asyncio
async def test_refresh_recovers_both_queues_even_without_replay_cursor():
    machine, transport, ctx = setup_queue("dsh")
    second = _mk_ctx("codex@other")
    second.engine = "codex"
    second.state = "running"
    machine.sessions[second.key] = second
    await machine._handle_client_hello(Hello(role="client", client_id="phone", cursors={}))
    queues = [frame for frame in transport.sent if frame.type == "query_queue"]
    assert {frame.sid for frame in queues} == {ctx.key, second.key}
    assert all(frame.to == "phone" for frame in queues)
    assert next(frame for frame in queues if frame.sid == ctx.key).items[0].msg_id == "queued-one"
    assert next(frame for frame in queues if frame.sid == second.key).items == []


def test_edit_actions_roundtrip_and_old_detail_shape_is_unchanged():
    ctx = SimpleNamespace(key="dsh@queue")
    assert deserialize(serialize(detail(ctx, "begin"))).edit_action == "begin"
    old = GetQueuedQuery(sid=ctx.key, msg_id="queued-one", cmd_id="old", client_id="phone")
    assert "edit_action" not in serialize(old)
