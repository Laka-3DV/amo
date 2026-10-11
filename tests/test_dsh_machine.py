"""Machine-level regressions for DSH mutation reconciliation."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import pytest

from cc_remote.protocol import (
    ERR_FORK_RECONCILING,
    ERR_BAD_PROMPT,
    ERR_NOT_STEERABLE,
    ERR_PROTOCOL,
    ERR_STEER_UNKNOWN,
    CommandAck,
    ContextReport,
    ConversationTurn,
    Error,
    ForkSession,
    GetContext,
    GetEngineCapabilities,
    GetModels,
    Interrupt,
    ListSessions,
    Models,
    NewSession,
    AnswerQuestion,
    ProcessEvent,
    Query,
    RenameSession,
    SetEffort,
    SetPerm,
    Steer,
    SwitchSession,
    TurnEnd,
    UserMsg,
    UpdateQueuedQuery,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.dsh_client import (
    DshRpcError,
    DshSessionHandle,
    DshUnavailable,
)
from cc_remote.wrapper.dsh_forks import DshForkJournalError
from cc_remote.wrapper.dsh_history import DshHistoryPage
from cc_remote.wrapper.dsh_stream import DshStreamTranslator
from tests.test_multisession import _mk_ctx, _mk_machine


class _DshClient:
    def __init__(
        self,
        *,
        running: bool = True,
        prompt_error: Exception | None = None,
        fork_child: str | None = None,
        fork_error: Exception | None = None,
        cancel_error: Exception | None = None,
        models_value: dict | None = None,
        history_value: dict | None = None,
    ) -> None:
        self.running = running
        self.prompt_error = prompt_error
        self.fork_child = fork_child
        self.fork_error = fork_error
        self.cancel_error = cancel_error
        self.models_value = models_value
        self.history_value = history_value
        self.calls: list[tuple[str, dict, str | None]] = []
        self.response_errors: list[tuple[str, str]] = []
        self.responses: list[tuple[str, dict]] = []
        self.declined: list[str] = []

    async def decline_waterfall(self, event_id: str) -> bool:
        self.declined.append(event_id)
        return True

    async def respond(self, rpc_id: str, value: dict) -> bool:
        self.responses.append((rpc_id, dict(value)))
        return True

    async def call(
        self,
        method: str,
        payload: dict | None = None,
        *,
        rpc_id: str | None = None,
        no_timeout: bool = False,
    ):
        assert no_timeout is False
        body = dict(payload or {})
        self.calls.append((method, body, rpc_id))
        if method == "session.prompt":
            if self.prompt_error is not None:
                raise self.prompt_error
            return {"queued": True}
        if method == "session.cancel":
            if self.cancel_error is not None:
                raise self.cancel_error
            return {"accepted": True}
        if method == "session.models" and self.models_value is not None:
            return self.models_value
        if method == "session.history":
            return self.history_value or {
                "events": [],
                "hasMore": False,
                "projections": {"asOfSeq": -1, "values": {}},
            }
        if method == "session.list":
            return {
                "items": [{
                    "sessionId": body.get("sessionId", "native-session"),
                    "running": self.running,
                }]
            }
        if method == "skill.list":
            return {"skills": [{
                "name": "repo-skill",
                "description": "Use the repository skill",
                "modelInvocable": True,
            }]}
        if method == "session.fork":
            if self.fork_error is not None:
                raise self.fork_error
            if self.fork_child is not None:
                return {"sessionId": self.fork_child}
        raise AssertionError(f"unexpected DSH method: {method}")

    async def respond_error(
        self,
        rpc_id: str,
        *,
        code: str = "cancelled",
        message: str = "Remote interaction was cancelled",
    ) -> bool:
        assert code == "cancelled"
        self.response_errors.append((rpc_id, message))
        return True


class _DshHistory:
    def __init__(self, page_factory: Callable[[], DshHistoryPage]) -> None:
        self.page_factory = page_factory
        self.calls: list[tuple[str, dict]] = []

    async def page(self, session_id: str, **kwargs) -> DshHistoryPage:
        self.calls.append((session_id, dict(kwargs)))
        return self.page_factory()


def _history_page(*client_message_ids: str) -> DshHistoryPage:
    return DshHistoryPage(
        events=(),
        turns=tuple(
            ConversationTurn(
                id=f"dsh-msg-{index}",
                clientMsgId=message_id,
                prompt="steer",
            )
            for index, message_id in enumerate(client_message_ids, start=1)
        ),
        has_more=False,
        oldest_id=("dsh-msg-1" if client_message_ids else None),
        newest_id=(f"dsh-msg-{len(client_message_ids)}"
                   if client_message_ids else None),
        last_seq=len(client_message_ids),
        projections={},
        projection_seq=len(client_message_ids),
        translator=DshStreamTranslator(),
    )


async def _install_dsh(machine, client: _DshClient, history: _DshHistory) -> None:
    original = machine._dsh_client
    if original is not None:
        await original.stop()
    machine._dsh_client = client
    machine._dsh_history = history
    machine._dsh_available = True
    machine.DSH_RECEIPT_RECONCILE_DELAYS = (0.0,)


@pytest.mark.asyncio
async def test_dsh_sidebar_hides_only_cold_blank_sessions():
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    resident = _mk_ctx("dsh@resident-blank", "resident-blank")
    resident.engine = "dsh"
    machine.sessions[resident.key] = resident

    async def catalog():
        return ([
            {
                "sessionId": "cold-blank",
                "updatedAt": 1,
                "running": False,
                "blank": True,
                "cwd": "/tmp/cold",
            },
            {
                "sessionId": "resident-blank",
                "updatedAt": 2,
                "running": False,
                "blank": True,
                "cwd": "/tmp/resident",
            },
            {
                "sessionId": "cold-materialized",
                "updatedAt": 3,
                "running": False,
                "blank": False,
                "cwd": "/tmp/materialized",
            },
        ], set())

    machine._read_dsh_catalog = catalog
    result = await machine._list_dsh_sessions(ListSessions(
        engine="dsh", space="code", client_id="browser",
    ))

    assert [row.session_id for row in result.sessions] == [
        "dsh@resident-blank",
        "dsh@cold-materialized",
    ]
    assert transport.sent[-1] is result


class _CommandClient(_DshClient):
    def __init__(self, *, execution_error: Exception | None = None) -> None:
        super().__init__()
        self.execution_error = execution_error
        self.command_started = asyncio.Event()
        self.release_command = asyncio.Event()

    async def call(
        self,
        method: str,
        payload: dict | None = None,
        *,
        rpc_id: str | None = None,
        no_timeout: bool = False,
    ):
        body = dict(payload or {})
        if method == "commands/list":
            assert no_timeout is False
            self.calls.append((method, body, rpc_id))
            return [{
                "name": "compact",
                "description": "Compact context",
                "input": {"hint": "optional focus"},
            }]
        if method == "commands/execute":
            assert no_timeout is True
            self.calls.append((method, body, rpc_id))
            self.command_started.set()
            await self.release_command.wait()
            if self.execution_error is not None:
                raise self.execution_error
            return {
                "commandId": "native-command-1",
                "result": {"kind": "success", "text": "Compacted"},
            }
        return await super().call(
            method,
            body,
            rpc_id=rpc_id,
            no_timeout=no_timeout,
        )


def _mux_command_event(
    event_type: str,
    seq: int,
    data: dict,
) -> dict:
    return {
        "rpcId": f"command-event-{seq}",
        "payload": {
            "type": "session/event",
            "sessionId": "native-session",
            "event": {
                "type": event_type,
                "seq": seq,
                "time": 1_700_000_000_000 + seq * 100,
                "data": data,
            },
        },
    }


def _install_running_session(machine, client: _DshClient, cwd: str = "/tmp"):
    ctx = _mk_ctx("dsh@native-session", "native-session")
    ctx.engine = "dsh"
    ctx.cwd = cwd
    ctx.state = "running"
    ctx.active_msg_id = "initial-message"
    ctx.sdk = DshSessionHandle(client, "native-session", cwd)
    machine.sessions[ctx.key] = ctx
    return ctx


@pytest.mark.asyncio
async def test_dsh_image_model_rejection_uses_explicit_user_copy():
    machine, _transport = _mk_machine()
    client = _DshClient(prompt_error=DshRpcError(
        "attachment-error",
        'Model "deepseek-v4-pro" does not support image input.',
        details={"reason": "MODEL_DOES_NOT_SUPPORT_IMAGES"},
    ))
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.state = "idle"
    ctx.active_msg_id = None

    result = await machine._handle_immediate_query(ctx, Query(
        sid=ctx.key,
        prompt="查看图片",
        images=[{
            "media_type": "image/png",
            "data": (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                "AAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
            ),
        }],
        msg_id="unsupported-image-message",
        cmd_id="query-command",
        client_id="client-1",
    ))

    assert isinstance(result, Error)
    assert result.code == ERR_BAD_PROMPT
    assert result.message == "当前 DSH 模型不支持图片输入。"
    assert ctx.state == "idle"
    assert client.calls[0][1]["content"][1]["type"] == "image"


@pytest.mark.asyncio
async def test_dsh_registered_command_runs_off_lane_and_reconciles_mux_once():
    machine, transport = _mk_machine()
    client = _CommandClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.state = "idle"
    ctx.active_msg_id = None

    result = await machine._handle_immediate_query(ctx, Query(
        sid=ctx.key,
        prompt="/compact now",
        msg_id="remote-command-message",
        cmd_id="query-command",
        client_id="client-1",
    ))
    await asyncio.wait_for(client.command_started.wait(), timeout=1)
    command_task = ctx.turn_task

    assert result is None
    assert command_task is not None and not command_task.done()
    assert ctx.state == "running"
    assert ctx.dsh_pending_command_msg_id == "remote-command-message"

    # A different DSH surface may issue a command while Remote's handler is
    # running.  It must remain visible; only the exact matching lifecycle is
    # folded into Remote's optimistic row.
    await machine._on_dsh_mux(_mux_command_event(
        "command/run",
        1,
        {
            "commandId": "external-command",
            "name": "feedback",
            "args": " hello",
            "source": {"kind": "user"},
        },
    ))
    await machine._on_dsh_mux(_mux_command_event(
        "command/done",
        2,
        {
            "commandId": "external-command",
            "kind": "success",
            "text": "Recorded",
        },
    ))
    assert any(
        isinstance(message, UserMsg) and message.prompt == "/feedback hello"
        for message in transport.sent
    )

    await machine._on_dsh_mux(_mux_command_event(
        "command/run",
        3,
        {
            "commandId": "native-command-1",
            "name": "compact",
            "args": " now",
            "source": {"kind": "user"},
        },
    ))
    await machine._on_dsh_mux(_mux_command_event(
        "command/done",
        4,
        {
            "commandId": "native-command-1",
            "kind": "success",
            "text": "Compacted",
        },
    ))
    client.release_command.set()
    await asyncio.wait_for(command_task, timeout=1)
    await asyncio.sleep(0)

    remote_users = [
        message for message in transport.sent
        if isinstance(message, UserMsg)
        and message.client_msg_id == "remote-command-message"
    ]
    remote_processes = [
        message for message in transport.sent
        if isinstance(message, ProcessEvent)
        and message.command == "/compact now"
    ]
    remote_terminals = [
        message for message in transport.sent
        if isinstance(message, TurnEnd)
        and message.presentation_id == "remote-command-message"
    ]
    assert len(remote_users) == 1
    assert [message.phase for message in remote_processes] == ["start", "end"]
    assert len(remote_terminals) == 1
    assert remote_terminals[0].turn_id is None
    assert ctx.dsh_command_aliases == {
        "native-command-1": "remote-command-message",
    }
    assert ctx.state == "idle"
    assert ctx.turn_task is None
    assert history.calls[-1][1]["command_aliases"] == {
        "native-command-1": "remote-command-message",
    }


@pytest.mark.asyncio
async def test_dsh_command_unknown_outcome_closes_every_client_projection():
    machine, transport = _mk_machine()
    client = _CommandClient(
        execution_error=DshUnavailable("receipt lost"),
    )
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.state = "idle"
    ctx.active_msg_id = None

    await machine._handle_immediate_query(ctx, Query(
        sid=ctx.key,
        prompt="/compact",
        msg_id="unknown-command-message",
        cmd_id="query-command",
        client_id="client-1",
    ))
    await asyncio.wait_for(client.command_started.wait(), timeout=1)
    task = ctx.turn_task
    assert task is not None
    client.release_command.set()
    await asyncio.wait_for(task, timeout=1)

    assert any(
        isinstance(message, UserMsg)
        and message.client_msg_id == "unknown-command-message"
        and message.prompt == "/compact"
        for message in transport.sent
    )
    assert any(
        isinstance(message, Error)
        and message.msg_id == "unknown-command-message"
        and "结果未知" in message.message
        for message in transport.sent
    )
    assert ctx.state == "idle"
    assert ctx.dsh_pending_command_msg_id is None
    assert ctx.dsh_pending_command_prompt is None


@pytest.mark.asyncio
async def test_dsh_shutdown_cancels_command_without_forging_user_interrupt():
    machine, transport = _mk_machine()
    client = _CommandClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.state = "idle"
    ctx.active_msg_id = None

    await machine._handle_immediate_query(ctx, Query(
        sid=ctx.key,
        prompt="/compact",
        msg_id="shutdown-command-message",
        cmd_id="query-command",
        client_id="client-1",
    ))
    await asyncio.wait_for(client.command_started.wait(), timeout=1)
    before = len(transport.sent)

    await machine._cancel_dsh_commands_for_shutdown()

    assert ctx.turn_task is None
    assert ctx.dsh_pending_command_msg_id is None
    assert ctx.dsh_pending_command_prompt is None
    assert not any(
        isinstance(message, (UserMsg, TurnEnd))
        for message in transport.sent[before:]
    )


def test_dsh_model_and_capability_reads_reject_cross_engine_scope():
    with pytest.raises(ValueError, match="only supported for DSH"):
        GetModels(engine="codex", session_id="dsh@native-session")
    with pytest.raises(ValueError, match="only supported for Codex"):
        GetModels(engine="dsh", codex_profile_id="default")
    with pytest.raises(ValueError, match="only supported in Code"):
        GetEngineCapabilities(engine="dsh", space="work")
    with pytest.raises(ValueError, match="only supported for Codex"):
        GetEngineCapabilities(
            engine="dsh",
            space="code",
            codex_profile_id="default",
        )
    with pytest.raises(ValueError, match="DSH session id"):
        GetModels(engine="dsh", session_id="native-session")
    with pytest.raises(ValueError, match="DSH engine"):
        SwitchSession(
            engine="claude", session_id="dsh@native-session"
        )
    with pytest.raises(ValueError, match="DSH engine"):
        RenameSession(
            engine="codex",
            session_id="dsh@native-session",
            title="wrong engine",
        )


@pytest.mark.asyncio
async def test_dsh_effort_requires_an_explicit_matching_engine_scope():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    _install_running_session(machine, client)

    result = await machine._handle_set_effort(SetEffort(
        sid="dsh@native-session",
        effort="high",
    ))

    assert isinstance(result, Error)
    assert result.code == ERR_PROTOCOL
    assert client.calls == []


@pytest.mark.asyncio
async def test_dsh_reconcile_reserves_history_sequence_before_source_io():
    machine, transport = _mk_machine()
    client = _DshClient()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()

    class OrderedHistory(_DshHistory):
        async def page(self, session_id: str, **kwargs) -> DshHistoryPage:
            self.calls.append((session_id, dict(kwargs)))
            if len(self.calls) == 1:
                first_started.set()
                await release_first.wait()
                return _history_page("older")
            second_started.set()
            await release_second.wait()
            return _history_page("newer")

    history = OrderedHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    reconcile = asyncio.create_task(machine._reconcile_dsh_resident(ctx))
    await first_started.wait()
    requested = asyncio.create_task(machine._build_dsh_history(
        ctx.key,
        before=None,
        limit=4,
        detail="summary",
    ))
    await second_started.wait()
    release_second.set()
    newest = await requested
    release_first.set()
    await reconcile

    emitted = [message for message in transport.sent if message.type == "history"]
    assert newest.build_seq == 2
    assert emitted[-1].build_seq == 1
    assert machine._history_build_sequences[ctx.key] == 2


@pytest.mark.asyncio
async def test_dsh_reconcile_does_not_treat_a_truncated_active_tail_as_idle():
    machine, transport = _mk_machine()
    client = _DshClient(running=True)
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.state = "idle"

    await machine._reconcile_dsh_resident(ctx)

    assert ctx.state == "running"
    pages = [event for event in transport.sent if event.type == "history"]
    assert pages[-1].in_progress is True
    assert any(
        method == "session.list" for method, _payload, _rpc in client.calls
    )


@pytest.mark.asyncio
async def test_dsh_live_acceptance_wins_over_an_older_idle_catalog_read():
    machine, transport = _mk_machine()

    class DelayedIdleClient(_DshClient):
        def __init__(self) -> None:
            super().__init__(running=False)
            self.list_started = asyncio.Event()
            self.release_list = asyncio.Event()

        async def call(self, method: str, payload=None, **kwargs):
            if method == "session.list":
                self.list_started.set()
                await self.release_list.wait()
            return await super().call(method, payload, **kwargs)

    client = DelayedIdleClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.dsh_uncertain_msg_id = "remote-message"

    reconciliation = asyncio.create_task(
        machine._reconcile_dsh_resident(
            ctx, expected_msg_id="remote-message",
        )
    )
    await client.list_started.wait()
    await machine._on_dsh_mux({
        "rpcId": "event-1",
        "payload": {
            "type": "session/event",
            "sessionId": "native-session",
            "event": {
                "type": "user/message",
                "seq": 1,
                "time": 1_700_000_000_000,
                "surfaceOp": "append",
                "data": {
                    "id": "native-message",
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                    "source": {"kind": "user", "rpcId": "remote-message"},
                },
            },
        },
    })
    client.release_list.set()

    assert await reconciliation is True
    assert ctx.dsh_uncertain_msg_id is None
    assert ctx.state == "running"
    pages = [event for event in transport.sent if event.type == "history"]
    assert pages[-1].in_progress is True


@pytest.mark.asyncio
async def test_dsh_context_read_reports_unavailable_before_projection_exists():
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    result = await machine._handle_get_context(GetContext(
        sid=ctx.key,
        cmd_id="context-command",
        client_id="client-1",
    ))

    assert isinstance(result, ContextReport)
    assert result.available is False
    assert result.total_tokens == 0
    assert result.max_tokens == 0
    assert result.percentage == 0.0
    # DSH 0.1.2 pushes projections on the control/follow streams, so a context
    # read must not issue a history RPC at all.
    assert client.calls == []
    assert transport.sent[-1] is result


@pytest.mark.asyncio
async def test_dsh_context_and_permissions_use_native_session_projections():
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    # DSH 0.2.0 delivers the current selection on the control/follow streams and
    # keeps the selectable presets in a process-level catalog.
    assert isinstance(ctx.sdk, DshSessionHandle)
    ctx.sdk.apply_permission_catalog({
        "options": [
            {"value": "workspace-write", "name": "workspace-write"},
            {"value": "danger-full-access", "name": "danger-full-access"},
        ],
    })
    ctx.sdk.apply_projection("permissions", {
        "currentValue": "workspace-write",
    }, 12)
    ctx.sdk.apply_projection("contextPressure", {
        "pressureTokens": 15000,
        "projectedTokens": 15854,
        "contextWindow": 1_000_000,
    }, 12)
    ctx.sdk.apply_projection("contextBreakdown", {
        "systemTokens": 8893,
        "toolsTokens": 224,
        "messageTokens": 4345,
    }, 12)

    result = await machine._handle_get_context(GetContext(
        sid=ctx.key,
        cmd_id="context-command",
        client_id="client-1",
    ))

    assert isinstance(result, ContextReport)
    assert result.available is None
    assert result.total_tokens == 15854
    assert result.max_tokens == 1_000_000
    assert result.percentage == pytest.approx(1.5854)
    assert [row["name"] for row in result.categories] == [
        "≈ 系统提示词", "≈ 工具", "≈ 消息",
    ]
    assert [row["tokens"] for row in result.categories] == [8893, 224, 4345]
    permission = next(
        event for event in transport.sent if event.type == "perm"
    )
    assert permission.mode == "workspace-write"
    assert [option.id for option in permission.options or ()] == [
        "workspace-write", "danger-full-access",
    ]
    assert [option.name for option in permission.options or ()] == [
        "Workspace Write", "Full Access",
    ]


@pytest.mark.asyncio
async def test_dsh_live_permission_projection_updates_the_session_control():
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    # The catalog supplies the options; the projection supplies the selection.
    ctx.sdk.apply_permission_catalog({
        "options": [
            {"value": "workspace-write", "name": "workspace-write"},
            {"value": "danger-full-access", "name": "danger-full-access"},
        ],
    })
    projection = {"currentValue": "danger-full-access"}

    await machine._on_dsh_mux({
        "rpcId": "projection-rpc",
        "payload": {
            "type": "session/projection",
            "sessionId": "native-session",
            "key": "permissions",
            "value": projection,
            "seq": 12,
        },
    })

    assert ctx.sdk.permission_mode == "danger-full-access"
    assert transport.sent[-1].type == "perm"
    assert transport.sent[-1].mode == "danger-full-access"
    assert transport.sent[-1].options[-1].name == "Full Access"
    sent = len(transport.sent)
    projection["currentValue"] = "workspace-write"
    await machine._on_dsh_mux({
        "rpcId": "older-projection-rpc",
        "payload": {
            "type": "session/projection",
            "sessionId": "native-session",
            "key": "permissions",
            "value": projection,
            "seq": 11,
        },
    })
    assert ctx.sdk.permission_mode == "danger-full-access"
    assert len(transport.sent) == sent


@pytest.mark.asyncio
async def test_dsh_permission_control_requires_engine_scope_and_confirms_projection():
    class PermissionClient(_DshClient):
        def __init__(self):
            super().__init__()
            self.current = "workspace-write"

        async def call(
            self, method, payload=None, *, rpc_id=None, no_timeout=False,
        ):
            body = dict(payload or {})
            self.calls.append((method, body, rpc_id))
            if method == "commands/execute":
                assert no_timeout is True
                assert body["line"] == "/permission danger-full-access"
                assert body["agentId"] == "native-session"
                assert body["submittedAttachments"] == []
                self.current = "danger-full-access"
                return {
                    "commandId": "permission-command",
                    "result": {"kind": "success", "sourceEventSeq": 20},
                }
            return await super().call(
                method, payload, rpc_id=rpc_id, no_timeout=no_timeout,
            )

    machine, transport = _mk_machine()
    client = PermissionClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.state = "idle"
    # The permissions projection arrives on the control/follow stream; DSH 0.2.0
    # carries only the selection, so the options come from the catalog.
    assert isinstance(ctx.sdk, DshSessionHandle)
    ctx.sdk.apply_permission_catalog({
        "options": [
            {"value": "workspace-write", "name": "workspace-write"},
            {"value": "danger-full-access", "name": "danger-full-access"},
        ],
    })
    ctx.sdk.apply_projection("permissions", {
        "currentValue": "workspace-write",
    }, 5)
    # Confirm the switch through the same stream once the command lands.
    original_execute = ctx.sdk.execute_command

    async def execute_and_project(line, *, rpc_id):
        result = await original_execute(line, rpc_id=rpc_id)
        if client.current != "workspace-write":
            ctx.sdk.apply_projection("permissions", {
                "currentValue": client.current,
            }, 6)
        return result

    ctx.sdk.execute_command = execute_and_project  # type: ignore[method-assign]

    result = await machine._handle_set_perm(SetPerm(
        sid=ctx.key,
        engine="dsh",
        mode="danger-full-access",
        cmd_id="permission-scoped",
        client_id="client-1",
    ))
    assert result.type == "perm"
    assert result.mode == "danger-full-access"
    assert result.options and result.options[-1].danger is True
    assert transport.sent[-1] is result


@pytest.mark.asyncio
async def test_dsh_skill_catalog_is_bound_to_the_requested_resident_session():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    result = await machine._handle_get_engine_capabilities(
        GetEngineCapabilities(
            engine="dsh",
            space="code",
            session_id=ctx.key,
            cwd=ctx.cwd,
            client_id="client-1",
        )
    )

    assert result.session_id == ctx.key
    assert [item.name for item in result.items] == ["repo-skill"]
    assert client.calls[-1] == (
        "skill.list", {"sessionId": "native-session"}, None
    )


@pytest.mark.asyncio
async def test_dsh_model_catalog_response_is_scoped_to_the_requested_session():
    machine, transport = _mk_machine()
    client = _DshClient(models_value={
        "current": {
            "provider": "deepseek-official",
            "model": "deepseek-v4-flash",
            "reasoningEffort": "high",
        },
        "groups": [{
            "id": "deepseek-official",
            "name": "DeepSeek",
            "models": [{
                "id": "deepseek-v4-flash",
                "name": "DeepSeek V4 Flash",
                "reasoning": {
                    "efforts": [{"id": "high"}],
                    "defaultEffort": "high",
                },
            }],
        }],
    })
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    result = await machine._handle_get_dsh_models(GetModels(
        engine="dsh",
        session_id=ctx.key,
        cmd_id="models-command",
        client_id="client-1",
    ))

    assert isinstance(result, Models)
    assert result.session_id == ctx.key
    assert result.default_model == (
        "dsh://deepseek-official/deepseek-v4-flash"
    )
    assert result.default_effort == "high"
    assert ctx.sdk.model == result.default_model
    assert ctx.sdk.effort == "high"
    assert client.calls == [(
        "session.models", {"sessionId": "native-session"}, None,
    )]
    assert all(
        getattr(message, "sid", None) == ctx.key
        for message in transport.sent[-2:]
    )


@pytest.mark.asyncio
async def test_dsh_interaction_overflow_fails_closed_without_spawning_a_task():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    _install_running_session(machine, client)
    machine.DSH_INTERACTION_TASK_CAP = 0

    await machine._on_dsh_mux({
        "rpcId": "approval-rpc",
        "payload": {
            "type": "approval/requested",
            "sessionId": "native-session",
            "approvalId": "approval-1",
            "toolName": "bash",
        },
    })

    assert client.response_errors == [(
        "approval-rpc", "Remote interaction capacity was reached",
    )]
    assert machine._dsh_interaction_tasks == set()
    assert machine._dsh_pending_approvals == {}


@pytest.mark.asyncio
async def test_dsh_question_resolution_is_scoped_to_one_session():
    machine, _transport = _mk_machine()
    first = _mk_ctx("first", "dsh@first")
    second = _mk_ctx("second", "dsh@second")
    first_wait = asyncio.get_running_loop().create_future()
    second_wait = asyncio.get_running_loop().create_future()
    first.pending_asks["first-ask"] = first_wait
    second.pending_asks["second-ask"] = second_wait
    machine._dsh_pending_request_asks = {
        ("dsh@first", "same-rpc"): {"first-ask"},
        ("dsh@second", "same-rpc"): {"second-ask"},
    }

    machine._cancel_dsh_request_asks(first, "dsh@first", "same-rpc")

    assert first_wait.done()
    assert first_wait.exception() is not None
    assert not second_wait.done()
    assert ("dsh@second", "same-rpc") in machine._dsh_pending_request_asks
    second_wait.cancel()


@pytest.mark.asyncio
async def test_interrupt_cancels_a_pending_question_without_answering_or_delegating():
    machine, transport = _mk_machine()
    client = _DshClient()
    await _install_dsh(machine, client, _DshHistory(lambda: _history_page()))
    ctx = _install_running_session(machine, client)
    other = _mk_ctx("dsh@other", "other")
    other_wait = asyncio.get_running_loop().create_future()
    other.pending_asks["other-ask"] = other_wait
    machine.sessions[other.key] = other
    ask_id = machine._dsh_ask_id(ctx.key, "question-rpc", "0:q1")
    question = asyncio.create_task(machine._answer_dsh_questions(ctx, {
        "rpcId": "question-rpc",
        "payload": {
            "type": "question/requested", "sessionId": "native-session",
            "questions": [{"id": "q1", "question": "继续吗？",
                           "options": [{"label": "继续"}, {"label": "换个方案"}]}],
        },
    }))
    try:
        for _ in range(20):
            if ask_id in ctx.pending_asks:
                break
            await asyncio.sleep(0)
        assert ask_id in ctx.pending_asks
        await machine._handle_interrupt(Interrupt(sid=ctx.key))
        await asyncio.wait_for(question, timeout=1)
        assert not ctx.pending_asks
        assert not other_wait.done()
        assert client.responses == []
        assert client.declined == []
        assert client.response_errors == [(
            "question-rpc", "Remote question interaction was cancelled",
        )]
        assert any(method == "session.cancel" for method, _, _ in client.calls)
        assert any(e.type == "ask_user_closed" and e.ask_id == ask_id
                   and e.reason == "cancelled" for e in transport.sent)
    finally:
        other_wait.cancel()
        if not question.done():
            question.cancel()
            await asyncio.gather(question, return_exceptions=True)


@pytest.mark.asyncio
async def test_dsh_interrupt_receipt_loss_does_not_force_a_running_turn_idle():
    machine, _transport = _mk_machine()
    client = _DshClient(cancel_error=DshUnavailable("receipt lost"))
    translator = DshStreamTranslator()
    translator.feed({
        "type": "turn/start",
        "seq": 1,
        "time": 1_700_000_000_000,
        "data": {"turn": 0},
    })
    page = _history_page()
    active_page = DshHistoryPage(
        events=page.events,
        turns=page.turns,
        has_more=page.has_more,
        oldest_id=page.oldest_id,
        newest_id=page.newest_id,
        last_seq=1,
        projections=page.projections,
        projection_seq=1,
        translator=translator,
    )
    history = _DshHistory(lambda: active_page)
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    await machine._handle_interrupt(Interrupt(
        sid=ctx.key,
        cmd_id="interrupt-command",
        client_id="client-1",
    ))
    for _ in range(20):
        if ctx.state == "running":
            break
        await asyncio.sleep(0)

    assert ctx.state == "running"
    assert client.calls == [(
        "session.cancel", {"sessionId": "native-session"}, None,
    )]


@pytest.mark.asyncio
async def test_dsh_lost_steer_receipt_accepts_only_exact_history_identity():
    machine, _transport = _mk_machine()
    client = _DshClient(prompt_error=DshUnavailable("receipt lost"))
    history = _DshHistory(lambda: _history_page("steer-message"))
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    result = await machine._handle_steer(Steer(
        sid=ctx.key,
        cmd_id="steer-command",
        client_id="client-1",
        prompt="continue",
        msg_id="steer-message",
    ))

    assert result is None
    assert ctx.dsh_uncertain_steer_id is None
    assert [call[0] for call in client.calls] == [
        "session.prompt", "session.list",
    ]


@pytest.mark.asyncio
async def test_dsh_lost_steer_receipt_rejects_when_idle_history_has_no_identity():
    machine, _transport = _mk_machine()
    client = _DshClient(
        running=False,
        prompt_error=DshUnavailable("receipt lost"),
    )
    history = _DshHistory(lambda: _history_page("another-message"))
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    result = await machine._handle_steer(Steer(
        sid=ctx.key,
        cmd_id="steer-command",
        client_id="client-1",
        prompt="continue",
        msg_id="steer-message",
    ))

    assert isinstance(result, Error)
    assert result.code == ERR_NOT_STEERABLE
    assert ctx.dsh_uncertain_steer_id is None
    assert [call[0] for call in client.calls] == [
        "session.prompt", "session.list",
    ]


@pytest.mark.asyncio
async def test_dsh_unknown_steer_stays_fail_closed_and_blocks_a_second_submit():
    machine, _transport = _mk_machine()
    client = _DshClient(prompt_error=DshUnavailable("receipt lost"))
    history = _DshHistory(lambda: _history_page("another-message"))
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    first = await machine._handle_steer(Steer(
        sid=ctx.key,
        cmd_id="steer-command",
        client_id="client-1",
        prompt="continue",
        msg_id="steer-message",
    ))
    second = await machine._handle_steer(Steer(
        sid=ctx.key,
        cmd_id="steer-command-2",
        client_id="client-1",
        prompt="do not duplicate",
        msg_id="steer-message-2",
    ))

    assert isinstance(first, Error) and first.code == ERR_STEER_UNKNOWN
    assert isinstance(second, Error) and second.code == ERR_STEER_UNKNOWN
    assert ctx.dsh_uncertain_steer_id == "steer-message"
    assert [call[0] for call in client.calls].count("session.prompt") == 1


@pytest.mark.asyncio
async def test_dsh_turn_end_does_not_guess_an_unknown_steer_was_accepted():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.dsh_uncertain_steer_id = "steer-message"

    await machine._on_dsh_mux({
        "rpcId": "event-1",
        "payload": {
            "type": "session/event",
            "sessionId": "native-session",
            "event": {
                "type": "turn/start", "seq": 1, "time": 1_700_000_000_000,
                "data": {"turn": 0},
            },
        },
    })
    await machine._on_dsh_mux({
        "rpcId": "event-2",
        "payload": {
            "type": "session/event",
            "sessionId": "native-session",
            "event": {
                "type": "turn/end", "seq": 2, "time": 1_700_000_001_000,
                "data": {"turn": 0, "reason": {"kind": "completed"}},
            },
        },
    })

    assert ctx.state == "idle"
    assert ctx.dsh_uncertain_steer_id == "steer-message"


@pytest.mark.asyncio
async def test_dsh_fork_is_not_acked_when_result_journal_fails_after_submit(
    monkeypatch,
    tmp_path,
):
    machine, transport = _mk_machine()
    client = _DshClient(fork_child="child-session")
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    machine.cfg.cc_cwd = str(tmp_path)

    async def catalog():
        return ([{
            "sessionId": "parent-session",
            "cwd": str(tmp_path),
            "origin": "user",
        }], set())

    machine._read_dsh_catalog = catalog
    journal = machine._dsh_forks
    assert journal is not None

    def fail_complete(_request_id: str, _session_id: str):
        raise DshForkJournalError("synthetic complete failure")

    monkeypatch.setattr(journal, "complete", fail_complete)
    command = ForkSession(
        session_id="dsh@parent-session",
        request_id="fork-request",
        last_turn_id="dsh-seq-20",
        cmd_id="fork-command",
        client_id="client-1",
    )

    with pytest.raises(machine_module._ForkOutcomeUncertain):
        await machine._process_command(command)

    entry = journal.get("fork-request")
    assert entry is not None and entry["status"] == "submitted"
    assert any(
        isinstance(message, Error) and message.code == ERR_FORK_RECONCILING
        for message in transport.sent
    )
    assert not any(isinstance(message, CommandAck) for message in transport.sent)
    assert [call[0] for call in client.calls] == ["session.fork"]


@pytest.mark.asyncio
async def test_dsh_fork_reconciliation_reports_unavailable_source_without_ack(
    monkeypatch,
    tmp_path,
):
    machine, transport = _mk_machine()
    client = _DshClient(fork_error=DshUnavailable("receipt lost"))
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    machine.cfg.cc_cwd = str(tmp_path)

    async def catalog():
        return ([{
            "sessionId": "parent-session",
            "cwd": str(tmp_path),
            "origin": "user",
        }], set())

    async def unavailable_candidate(_entry, *, attempts=1):
        assert attempts == 4
        raise DshUnavailable("catalog offline")

    machine._read_dsh_catalog = catalog
    monkeypatch.setattr(
        machine,
        "_dsh_fork_candidate",
        unavailable_candidate,
    )
    command = ForkSession(
        session_id="dsh@parent-session",
        request_id="fork-unavailable",
        last_turn_id="dsh-seq-20",
        cmd_id="fork-command",
        client_id="client-1",
    )

    with pytest.raises(machine_module._ForkOutcomeUncertain):
        await machine._process_command(command)

    journal = machine._dsh_forks
    assert journal is not None
    entry = journal.get("fork-unavailable")
    assert entry is not None and entry["status"] == "uncertain"
    assert any(
        isinstance(message, Error) and message.code == ERR_FORK_RECONCILING
        for message in transport.sent
    )
    assert not any(isinstance(message, CommandAck) for message in transport.sent)


@pytest.mark.asyncio
async def test_dsh_fork_reconciliation_reads_the_durable_cut_not_moving_tail(
    monkeypatch,
):
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)

    async def catalog():
        return ([{
            "sessionId": "child-session",
            "parentSessionId": "parent-session",
            "origin": "user",
        }], set())

    calls: list[tuple[str, dict]] = []

    async def call(method: str, payload=None, *, rpc_id=None):
        assert rpc_id is None
        body = dict(payload or {})
        calls.append((method, body))
        assert method == "session.history"
        return {
            "events": [{
                "event": {"type": "turn/end", "seq": 20},
            }],
            "hasMore": True,
        }

    machine._read_dsh_catalog = catalog
    monkeypatch.setattr(client, "call", call)

    child = await machine._dsh_fork_candidate({
        "baseline_session_ids": ["parent-session"],
        "native_parent_session_id": "parent-session",
        "at_seq": 20,
    })

    assert child == "child-session"
    assert calls == [("session.history", {
        "sessionId": "child-session",
        "beforeSeq": 21,
        "maxMessages": 1,
    })]


# ---- prompts settled by another client, and workspace grouping -------------


class _WorkspaceClient(_DshClient):
    """Fake that answers the create path, with an optional workspace failure."""

    def __init__(self, *, workspace_error: Exception | None = None) -> None:
        super().__init__()
        self.workspace_error = workspace_error
        self.workspace_calls: list[dict] = []

    async def call(self, method, payload=None, *, rpc_id=None, no_timeout=False):
        if method == "workspace.create":
            body = dict(payload or {})
            self.workspace_calls.append(body)
            self.calls.append((method, body, rpc_id))
            if self.workspace_error is not None:
                raise self.workspace_error
            return {
                "workspace": {
                    "workspaceId": "workspace-1",
                    "path": body.get("path"),
                    "title": "repo",
                },
                "created": True,
            }
        if method == "session.create":
            body = dict(payload or {})
            self.calls.append((method, body, rpc_id))
            return {"sessionId": body.get("sessionId"), "agentPreset": "standard"}
        if method == "session.list":
            body = dict(payload or {})
            return {"items": [{
                "sessionId": body.get("sessionId", "native-session"),
                "cwd": WORKSPACE_CWD,
                "running": False,
            }]}
        return await super().call(
            method, payload, rpc_id=rpc_id, no_timeout=no_timeout)


WORKSPACE_CWD = "/tmp/cc-remote-workspace"


@pytest.mark.asyncio
async def test_dsh_new_session_is_created_inside_its_workspace(tmp_path):
    """A cwd-only session lands in DSH's 未分组 forever: no API can regroup it."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    machine, _transport = _mk_machine()
    client = _WorkspaceClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)

    await machine._handle_new_session(NewSession(
        cwd=str(cwd), engine="dsh", request_id="req-new", client_id="client-1",
    ))

    assert client.workspace_calls == [{"path": str(cwd)}], (
        "the directory must be registered as a workspace first"
    )
    created = next(body for method, body, _ in client.calls
                   if method == "session.create")
    assert created["workspaceId"] == "workspace-1"
    assert "cwd" not in created, "DSH rejects workspaceId together with cwd"


@pytest.mark.asyncio
async def test_dsh_new_session_falls_back_to_cwd_when_workspace_fails(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    machine, _transport = _mk_machine()
    client = _WorkspaceClient(workspace_error=DshUnavailable("no registry"))
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)

    await machine._handle_new_session(NewSession(
        cwd=str(cwd), engine="dsh", request_id="req-new", client_id="client-1",
    ))

    created = next(body for method, body, _ in client.calls
                   if method == "session.create")
    assert created["cwd"] == str(cwd), "an ungrouped session still must start"


@pytest.mark.asyncio
async def test_dsh_approval_request_without_an_id_still_asks_the_user():
    """DSH 0.1.2 sends only toolName/callId/reason — no approval id at all.

    Requiring one raised DshProtocolError, so no card was shown, no answer was
    sent, and the agent stayed blocked forever.
    """
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    await machine._on_dsh_mux({
        "rpcId": "approval-rpc",
        "payload": {
            "type": "approval/requested",
            "sessionId": "native-session",
            "toolName": "bash",
            "callId": "call-1",
            "reason": "writes outside the workspace",
        },
    })
    await asyncio.sleep(0)

    asks = [event for event in transport.sent if event.type == "ask_user"]
    assert asks, "the user must be asked before a tool runs"
    assert "bash" in asks[-1].question
    assert machine._dsh_pending_approvals == {
        ("dsh@native-session", "call-1"): "approval-rpc",
    }
    for task in tuple(ctx.pending_asks.values()):
        task.cancel()
    for task in tuple(machine._dsh_interaction_tasks):
        task.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_dsh_approval_decided_elsewhere_closes_the_prompt():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    wait = asyncio.get_running_loop().create_future()
    ctx.pending_asks["ask-1"] = wait
    machine._dsh_pending_approvals = {
        ("dsh@native-session", "call-1"): "rpc-1",
    }
    machine._dsh_pending_request_asks = {
        ("dsh@native-session", "rpc-1"): {"ask-1"},
    }
    # The same prompt was cached before its session was resident: DSH never
    # resends it, so a stale cache entry is what re-prompted the phone.
    machine._pending_waterfalls = {"dsh@native-session": [
        {"rpcId": "rpc-1", "payload": {"type": "approval/requested"}},
    ]}

    async def event(kind: str, data: dict, seq: int) -> None:
        await machine._on_dsh_mux({
            "rpcId": f"evt-{seq}",
            "payload": {
                "type": "session/event",
                "sessionId": "native-session",
                "event": {
                    "type": kind, "seq": seq,
                    "time": 1_700_000_000_000 + seq, "data": data,
                },
            },
        })

    await event("approval/asked", {
        "id": "audit-1", "toolName": "bash", "callId": "call-1",
    }, 9)
    await event("approval/decided", {
        "id": "audit-1", "outcome": "allowed-once",
    }, 10)

    assert wait.done(), "the desktop's answer must close the phone's card"
    assert machine._dsh_pending_approvals == {}
    assert machine._pending_waterfalls == {}, "a settled prompt must not replay"
    ctx.pending_asks.pop("ask-1", None)


@pytest.mark.asyncio
async def test_dsh_ask_tool_completion_closes_an_answered_question():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    wait = asyncio.get_running_loop().create_future()
    ctx.pending_asks["ask-2"] = wait
    machine._dsh_pending_request_asks = {
        ("dsh@native-session", "question-rpc"): {"ask-2"},
    }
    # The live path records which tool call raised the question; its result is
    # the only durable proof DSH gives that the prompt was answered.
    machine._dsh_request_calls = {
        ("dsh@native-session", "question-rpc"): "call-9",
    }
    machine._pending_waterfalls = {"dsh@native-session": [
        {"rpcId": "question-rpc", "payload": {"type": "question/requested"}},
    ]}

    async def event(kind: str, data: dict, seq: int) -> None:
        await machine._on_dsh_mux({
            "rpcId": f"evt-{seq}",
            "payload": {
                "type": "session/event",
                "sessionId": "native-session",
                "event": {
                    "type": kind, "seq": seq,
                    "time": 1_700_000_000_000 + seq, "data": data,
                },
            },
        })

    await event("tool/call", {
        "turn": 0, "step": 0, "callId": "call-9",
        "name": "ask_user_question", "arguments": "{}",
    }, 20)
    assert not wait.done(), "an unrelated tool result must not close it"

    await event("tool/result", {
        "turn": 0, "step": 0,
        "message": {"source": {"callId": "call-9"}, "content": []},
    }, 21)

    assert wait.done(), "the answered question's tool call settles the prompt"
    assert machine._pending_waterfalls == {}
    ctx.pending_asks.pop("ask-2", None)


@pytest.mark.asyncio
async def test_dsh_turn_end_closes_a_question_that_never_reported_a_result():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    wait = asyncio.get_running_loop().create_future()
    ctx.pending_asks["ask-3"] = wait
    machine._dsh_pending_request_asks = {
        ("dsh@native-session", "question-rpc"): {"ask-3"},
    }

    await machine._on_dsh_mux({
        "rpcId": "evt-end",
        "payload": {
            "type": "session/event",
            "sessionId": "native-session",
            "event": {
                "type": "turn/end",
                "seq": 30,
                "time": 1_700_000_000_030,
                "data": {"turn": 0, "reason": {"kind": "aborted"}},
            },
        },
    })

    assert wait.done(), "an ended turn cannot still be waiting for an answer"
    ctx.pending_asks.pop("ask-3", None)


def _held_question(held_at: float | None = None) -> dict:
    frame = {
        "rpcId": "held-question",
        "payload": {
            "type": "question/requested",
            "sessionId": "native-session",
            "questions": [{
                "id": "q1",
                "question": "继续吗？",
                "options": [{"label": "继续"}, {"label": "停下"}],
            }],
        },
    }
    if held_at is not None:
        frame["_held_at"] = held_at
    return frame


@pytest.mark.asyncio
async def test_dsh_stale_held_prompt_is_not_replayed():
    """Replaying a days-old prompt forced the user to answer it again."""
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    machine._pending_waterfalls = {"dsh@native-session": [
        _held_question(held_at=time.time() - machine.HELD_WATERFALL_TTL - 60),
    ]}

    await machine._flush_pending_waterfalls(ctx)

    assert not [e for e in transport.sent if e.type == "ask_user"], (
        "an expired prompt must never be resurrected"
    )
    assert machine._pending_waterfalls == {}


@pytest.mark.asyncio
async def test_dsh_held_prompt_for_an_idle_session_is_not_replayed():
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.state = "idle"
    machine._pending_waterfalls = {"dsh@native-session": [
        _held_question(held_at=time.time()),
    ]}

    await machine._flush_pending_waterfalls(ctx)

    assert not [e for e in transport.sent if e.type == "ask_user"], (
        "an idle agent cannot still be waiting for an answer"
    )
    assert machine._pending_waterfalls == {}


@pytest.mark.asyncio
async def test_dsh_fresh_held_prompt_for_a_running_session_is_replayed():
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    machine._pending_waterfalls = {"dsh@native-session": [
        _held_question(held_at=time.time()),
    ]}

    await machine._flush_pending_waterfalls(ctx)
    await asyncio.sleep(0)  # the ask is raised on the interaction task lane

    assert [e for e in transport.sent if e.type == "ask_user"], (
        "the attach race this cache exists for must still work"
    )
    for task in tuple(ctx.pending_asks.values()):
        task.cancel()
    for task in tuple(machine._dsh_interaction_tasks):
        task.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_skipping_a_question_hands_it_back_to_dsh():
    """Dismissing must never be recorded as an answer the user did not give."""
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ask_id = machine._dsh_ask_id("dsh@native-session", "question-rpc", "0:q1")

    task = asyncio.create_task(machine._answer_dsh_questions(ctx, {
        "rpcId": "question-rpc",
        "payload": {
            "type": "question/requested", "sessionId": "native-session",
            "questions": [{
                "id": "q1", "question": "继续吗？",
                "options": [{"label": "继续"}, {"label": "停下"}],
            }],
        },
    }))
    for _ in range(20):
        if ctx.pending_asks.get(ask_id) is not None:
            break
        await asyncio.sleep(0)
    assert ctx.pending_asks.get(ask_id) is not None, "the card must be pending"

    await machine._handle_answer_question(AnswerQuestion(
        sid=ctx.key, ask_id=ask_id, outcome="skip",
    ))
    await task

    assert client.declined == ["question-rpc"], (
        "a skip must be delegated, not answered"
    )
    assert client.responses == []
    assert not [e for e in transport.sent if e.type == "error"]


@pytest.mark.asyncio
async def test_skipping_an_approval_delegates_instead_of_rejecting():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    machine._dsh_pending_request_asks = {
        ("dsh@native-session", "approval-rpc"): {"ask-1"},
    }
    ask_id = machine._dsh_ask_id("dsh@native-session", "approval-rpc", "call-1")

    task = asyncio.create_task(machine._answer_dsh_approval(ctx, {
        "rpcId": "approval-rpc",
        "payload": {
            "type": "approval/requested", "sessionId": "native-session",
            "toolName": "bash", "callId": "call-1",
        },
    }, "call-1"))
    for _ in range(10):
        if ctx.pending_asks.get(ask_id) is not None:
            break
        await asyncio.sleep(0)

    await machine._handle_answer_question(AnswerQuestion(
        sid=ctx.key, ask_id=ask_id, outcome="skip",
    ))
    await task

    assert client.declined == ["approval-rpc"]
    assert client.responses == [], (
        "a dismissal must not be sent as a rejection the user did not choose"
    )


@pytest.mark.asyncio
async def test_attaching_redelivers_a_prompt_the_session_is_waiting_on():
    """A reloaded page must see the card again, or the agent blocks invisibly."""
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ask_id = machine._dsh_ask_id("dsh@native-session", "question-rpc", "0:q1")

    task = asyncio.create_task(machine._answer_dsh_questions(ctx, {
        "rpcId": "question-rpc",
        "payload": {
            "type": "question/requested", "sessionId": "native-session",
            "questions": [{
                "id": "q1", "question": "继续吗？",
                "options": [{"label": "继续"}, {"label": "停下"}],
            }],
        },
    }))
    for _ in range(20):
        if ctx.pending_asks.get(ask_id) is not None:
            break
        await asyncio.sleep(0)
    transport.sent.clear()

    resent = await machine._resend_pending_asks(ctx.key, "client-after-reload")
    await asyncio.sleep(0)

    assert resent == 1
    asks = [event for event in transport.sent if event.type == "ask_user"]
    assert len(asks) == 1, "the pending prompt must be delivered to the new page"
    assert asks[0].ask_id == ask_id, "the same identity: answers correlate on it"
    assert asks[0].to == "client-after-reload"
    assert "继续" in [option["label"] for option in asks[0].options]

    # Once the ask resolves, nothing is re-delivered.
    ctx.pending_asks.pop(ask_id, None)
    ctx.pending_ask_specs.pop(ask_id, None)
    transport.sent.clear()
    assert await machine._resend_pending_asks(ctx.key, "client-2") == 0
    assert not [e for e in transport.sent if e.type == "ask_user"]
    task.cancel()


@pytest.mark.asyncio
async def test_dsh_assistant_stream_frames_never_advance_the_durable_watermark():
    """DSH 0.2.0 live deltas are process-local and carry no Session seq.

    They must reach the client without touching ``dsh_last_source_seq``;
    advancing it would make the next real journal event look stale and drop
    the authoritative content that supersedes the stream.
    """
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    await machine._on_dsh_mux({"payload": {
        "type": "session/event",
        "sessionId": "native-session",
        "event": {
            "type": "turn/start", "seq": 10,
            "time": 1_700_000_000_000, "data": {"turn": 0},
        },
    }})
    assert ctx.dsh_last_source_seq == 10
    transport.sent.clear()

    for index, chunk in enumerate((
        {"type": "block-start", "index": 0, "blockType": "text"},
        {"type": "text-delta", "index": 0, "text": "streamed"},
    )):
        await machine._on_dsh_mux({"payload": {
            "type": "session/assistant-stream",
            "sessionId": "native-session",
            "turn": 0,
            "step": 0,
            "index": index,
            "time": 1_700_000_000_001 + index,
            "chunk": chunk,
        }})

    assert ctx.dsh_last_source_seq == 10, "stream frames must not move it"
    deltas = [m for m in transport.sent if m.type == "delta"]
    assert [m.text for m in deltas] == ["streamed"]

    # The durable settlement still lands and still advances the watermark.
    await machine._on_dsh_mux({"payload": {
        "type": "session/event",
        "sessionId": "native-session",
        "event": {
            "type": "assistant/message", "seq": 11,
            "time": 1_700_000_000_010, "surfaceOp": "append",
            "data": {
                "turn": 0, "step": 0,
                "message": {
                    "id": "assistant-1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "streamed"}],
                    "source": {
                        "kind": "model",
                        "provider": "deepseek-official",
                        "model": "deepseek-flash",
                    },
                },
            },
        },
    }})
    assert ctx.dsh_last_source_seq == 11
    assert [m.type for m in transport.sent].count("assistant_msg_end") == 1


@pytest.mark.asyncio
async def test_dsh_assistant_stream_frame_without_a_translator_is_ignored():
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.dsh_translator = None
    watermark = ctx.dsh_last_source_seq
    transport.sent.clear()

    await machine._on_dsh_mux({"payload": {
        "type": "session/assistant-stream",
        "sessionId": "native-session",
        "turn": 0, "step": 0, "index": 0,
        "time": 1_700_000_000_001,
        "chunk": {"type": "text-delta", "index": 0, "text": "orphan"},
    }})

    assert [m for m in transport.sent if m.type == "delta"] == []
    assert ctx.dsh_last_source_seq == watermark


@pytest.mark.asyncio
async def test_dsh_model_selection_projection_announces_model_and_effort():
    """A resume must publish the live route and strength on its own.

    DSH 0.2.0 reports the session's route only through the ``modelSelection``
    projection; without consuming it the composer keeps showing "强度读取中"
    until the user switches model by hand.
    """
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    transport.sent.clear()

    await machine._on_dsh_mux({"payload": {
        "type": "session/projection",
        "sessionId": "native-session",
        "key": "modelSelection",
        "value": {
            "lastUsed": {
                "provider": "deepseek-official", "model": "deepseek-flash",
                "reasoningEffort": "low",
            },
            "next": {
                "provider": "deepseek-official", "model": "deepseek-flash",
                "reasoningEffort": "max",
            },
        },
        "seq": 14,
    }})

    models = [m for m in transport.sent if m.type == "model"]
    efforts = [m for m in transport.sent if m.type == "effort"]
    assert [m.model for m in models] == ["dsh://deepseek-official/deepseek-flash"]
    assert [m.effort for m in efforts] == ["max"]
    assert ctx.announced_model == "dsh://deepseek-official/deepseek-flash"
    assert ctx.announced_effort == "max"

    # An older revision never overwrites the newer readout.
    sent = len(transport.sent)
    await machine._on_dsh_mux({"payload": {
        "type": "session/projection",
        "sessionId": "native-session",
        "key": "modelSelection",
        "value": {
            "next": {
                "provider": "deepseek-official", "model": "deepseek-flash",
                "reasoningEffort": "off",
            },
        },
        "seq": 13,
    }})
    assert len(transport.sent) == sent
    assert ctx.announced_effort == "max"


@pytest.mark.asyncio
async def test_dsh_permission_catalog_change_reannounces_every_resident():
    """DSH signals the catalog changed with no payload; the client re-reads it."""
    machine, transport = _mk_machine()

    class CatalogClient(_DshClient):
        def __init__(self):
            super().__init__()
            self.options = [
                {"value": "workspace-write", "name": "workspace-write"},
            ]
            self.permission_options = ()

        async def refresh_permission_catalog(self) -> bool:
            self.calls.append(("permissionPresets/catalog", {}, None))
            parsed = DshSessionHandle._permission_catalog(
                {"options": list(self.options)}
            )
            if parsed == self.permission_options:
                return False
            self.permission_options = parsed
            return True

    client = CatalogClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    # The session already has a selection; only the option list changes.
    ctx.sdk.apply_projection(
        "permissions", {"currentValue": "workspace-write"}, 3,
    )
    client.permission_options = ctx.sdk.permission_options
    transport.sent.clear()

    client.options = [
        {"value": "workspace-write", "name": "workspace-write"},
        {"value": "danger-full-access", "name": "danger-full-access"},
    ]
    await machine._on_dsh_host({"payload": {
        "type": "permission-presets/catalog-changed",
    }})

    assert [option["id"] for option in ctx.sdk.permission_options] == [
        "workspace-write", "danger-full-access",
    ]
    assert [option["name"] for option in ctx.sdk.permission_options] == [
        "Workspace Write", "Full Access",
    ]
    perms = [m for m in transport.sent if m.type == "perm"]
    assert [option.id for option in perms[-1].options] == [
        "workspace-write", "danger-full-access",
    ]


@pytest.mark.asyncio
async def test_dsh_roster_burst_collapses_to_one_read_and_expires():
    """DSH 0.2.0's unfilterable ~2 MB roster must not be read per caller.

    One entry triggers several reads back to back (sidebar, then attach), so the
    short single-flight window is what keeps that from costing several round
    trips. It must also expire, or the sidebar would never see a new session.
    """
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)

    def list_reads() -> int:
        return len([c for c in client.calls if c[0] == "session.list"])

    await machine._read_dsh_catalog()
    assert list_reads() == 1

    # A burst inside the window rides the cache.
    await machine._read_dsh_catalog()
    await machine._read_dsh_catalog()
    assert list_reads() == 1

    # Callers get copies, so a mutation cannot poison the cache.
    rows, _archived = await machine._read_dsh_catalog()
    if rows:
        rows[0]["sessionId"] = "mutated"
    again, _archived = await machine._read_dsh_catalog()
    assert all(row.get("sessionId") != "mutated" for row in again)

    # Past the window it reads again.
    machine.DSH_CATALOG_TTL_SECONDS = 0.0
    await machine._read_dsh_catalog()
    assert list_reads() == 2


@pytest.mark.asyncio
async def test_dsh_roster_cache_coalesces_concurrent_readers():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)

    await asyncio.gather(*(
        machine._read_dsh_catalog() for _ in range(6)
    ))
    reads = len([c for c in client.calls if c[0] == "session.list"])
    assert reads == 1, f"six concurrent readers made {reads} roster reads"


def test_dsh_history_error_message_distinguishes_an_unreadable_log():
    """A format-v0 refusal is permanent; a carrier failure is worth retrying."""
    from cc_remote.wrapper.dsh_client import DshRpcError
    from cc_remote.wrapper.machine import _dsh_history_error_message

    unreadable = [
        'failed to observe session "s": turn/start 5 does not close the prior '
        "turn; source v0 artifact remains unchanged",
        "@deepseek-ai/dsh-session-format-v0-to-v1 refuses this format v0 "
        "Session: bad member",
        'failed to observe session "s": cannot safely transform unclassified '
        "message source; source v0 artifact remains unchanged",
    ]
    for message in unreadable:
        text = _dsh_history_error_message(DshRpcError("gateway/internal", message))
        assert "旧格式" in text
        assert "重试无效" in text
        assert "请稍后重试" not in text

    transient = _dsh_history_error_message(
        DshRpcError("gateway/internal", "carrier hiccup"))
    assert "请稍后重试" in transient


@pytest.mark.asyncio
async def test_start_queued_now_preserves_payload_and_waits_for_idle(monkeypatch):
    machine, transport = _mk_machine()
    client = _DshClient()
    ctx = _install_running_session(machine, client)
    before = Query(sid=ctx.key, msg_id="before", prompt="before", delivery="queue", cmd_id="enqueue-before", client_id="phone")
    image = {"media_type": "image/png", "data": "aGVsbG8="}
    chosen = Query(sid=ctx.key, msg_id="chosen", prompt="original", delivery="queue", images=[image], cmd_id="enqueue-chosen", client_id="phone")
    ctx.queued_queries = [before, chosen]
    ctx.queued_query_bytes = sum(machine._queued_query_size(q) for q in ctx.queued_queries)
    machine._queued_query_count = 2
    machine._queued_query_bytes = ctx.queued_query_bytes
    monkeypatch.setattr(machine, "_schedule_query_queue_drain", lambda _ctx: None)
    result = await machine._handle_update_queued_query(UpdateQueuedQuery(
        sid=ctx.key, msg_id="chosen", prompt="edited", start_now=True,
        cmd_id="start-chosen", client_id="phone"))
    assert result.updated
    assert [q.msg_id for q in ctx.queued_queries] == ["chosen", "before"]
    assert ctx.queued_queries[0].prompt == "edited"
    assert ctx.queued_queries[0].images == [image]
    assert machine._queued_query_count == 2
    assert [call[0] for call in client.calls] == ["session.cancel"]
    assert ctx.state == "interrupting"
    assert machine._query_queue_state(ctx).can_start_now is True


@pytest.mark.asyncio
async def test_start_queued_now_cannot_cancel_a_different_turn_after_item_started(monkeypatch):
    machine, _transport = _mk_machine()
    client = _DshClient()
    ctx = _install_running_session(machine, client)
    monkeypatch.setattr(machine, "_schedule_query_queue_drain", lambda _ctx: None)
    result = await machine._handle_update_queued_query(UpdateQueuedQuery(
        sid=ctx.key, msg_id="already-started", prompt="edited", start_now=True,
        cmd_id="late-start", client_id="phone"))
    assert not result.updated
    assert client.calls == []
    assert ctx.state == "running"

@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["dsh", "codex"])
async def test_queue_attachment_edit_preserves_originals_and_rejects_bad_batch(monkeypatch, engine):
    import base64
    from cc_remote.protocol import deserialize, serialize
    machine, transport = _mk_machine()
    client = _DshClient()
    ctx = _install_running_session(machine, client)
    ctx.engine = engine
    original = {"filename": "old.txt", "data": base64.b64encode(b"original").decode()}
    addition = {"filename": "new.txt", "data": base64.b64encode(b"new context").decode()}
    image = {"media_type": "image/png", "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg=="}
    queued = Query(sid=ctx.key, msg_id="attachment-edit", prompt="original", delivery="queue",
        files=[original], cmd_id="enqueue", client_id="phone")
    ctx.queued_queries = [queued]
    ctx.queued_query_bytes = machine._queued_query_bytes = machine._queued_query_size(queued)
    machine._queued_query_count = 1
    monkeypatch.setattr(machine, "_schedule_query_queue_drain", lambda _: None)
    command = UpdateQueuedQuery(sid=ctx.key, msg_id=queued.msg_id, prompt="updated",
        add_images=[image], add_files=[addition], cmd_id="edit-attachments", client_id="phone")
    assert deserialize(serialize(command)) == command
    result = await machine._handle_update_queued_query(command)
    assert result.updated and client.calls == [] and ctx.state == "running"
    saved = ctx.queued_queries[0]
    assert saved.prompt == "updated" and saved.files == [original, addition] and saved.images == [image]
    assert machine._queued_query_bytes == machine._queued_query_size(saved)
    projected = machine._query_queue_state(ctx)
    assert projected.items[0].file_count == 2 and projected.items[0].image_count == 1
    # Neither a broken image nor a too-large combined batch changes the queue.
    for kwargs in ({"add_images": [{"media_type": "image/png", "data": "aGVsbG8="}]},
                   {"add_files": [addition] * 7}):
        rejected = await machine._handle_update_queued_query(UpdateQueuedQuery(
            sid=ctx.key, msg_id=queued.msg_id, prompt="must not commit", **kwargs,
            cmd_id="reject-attachments", client_id="phone"))
        assert not rejected.updated
        assert ctx.queued_queries[0] == saved
        assert machine._queued_query_bytes == machine._queued_query_size(saved)
