"""Codex adapter sharing Remote's sessions, bounded FIFO and display protocol."""
from __future__ import annotations

import asyncio
import json
import re
import time
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from cc_remote.attachments import validate_attachments
from cc_remote.protocol import (
    AskUser, AskUserClosed, ContextReport, Effort, EngineCapabilities, Error,
    History, Model, Models, Perm, PermissionModeInfo, ProcessEvent,
    SessionControl, SessionFocus, SessionForked, SessionInfo, SessionList, Snapshot,
    StateEvent, ToolDelta, TurnBinding, TurnDetail, UserMsg, Delta, AssistantMsgEnd, HistoryImage,
    ERR_AUTH, ERR_BAD_PROMPT, ERR_BUSY, ERR_NOT_RUNNING, ERR_INTERNAL, ERR_CC_CRASH,
)
from .codex_client import CodexClient, CodexRpcError, CodexUnavailable
from .codex_stream import item_events, history_events, terminal_event, turn_failure, user_images, image_id
from .codex_lifecycle import NativeLifecycle, apply_lifecycle
from .codex_questions import TOOL_NAME, USER_INPUT_TOOL, validated_questions
from .history_store import materialize_history_turns
from .ringbuffer import RingBuffer
from .session_ctx import SessionContext
from .session_pins import SessionPinStore, SessionPinStoreError
from .session_presentation import SessionPresentationStore, SessionPresentationStoreError

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,239}$")
log = logging.getLogger(__name__)
DESKTOP_CONTROL = "当前会话由其他 Codex 界面控制；可查看内容，或从回复派生新会话继续。"


def writer_conflict(exc):
    return isinstance(exc, CodexRpcError) and "already has an active writer" in str(exc).lower()
PERMISSIONS = [
    PermissionModeInfo(id="read-only", name="只读", description="读取文件；执行需确认"),
    PermissionModeInfo(id="workspace-write", name="项目写入", description="修改当前项目；额外权限需确认"),
    PermissionModeInfo(id="danger-full-access", name="完整访问", description="允许本机完整访问，不请求审批", danger=True),
]


def native_id(value: str) -> str:
    result = value.removeprefix("codex@")
    if not value.startswith("codex@") or not _ID.fullmatch(result):
        raise ValueError("Codex 会话标识无效")
    return result


@dataclass
class CodexHandle:
    bridge: object
    session_id: str
    model: str | None = None
    effort: str | None = None
    approval: str = "on-request"
    sandbox: dict = field(default_factory=lambda: {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
                                                  "excludeTmpdirEnvVar": False, "excludeSlashTmp": False})
    writable: bool = True
    turn_id: str | None = None
    pending_query: object | None = None
    turn_messages: OrderedDict = field(default_factory=OrderedDict)
    streamed: dict[str, int] = field(default_factory=dict)
    started_items: set[str] = field(default_factory=set)
    closed_items: set[str] = field(default_factory=set)
    finished_turns: OrderedDict = field(default_factory=OrderedDict)
    completion: asyncio.Event = field(default_factory=asyncio.Event)
    token_usage: dict | None = None
    stop_requested: bool = False
    native_thread: dict | None = None
    mirror_task: asyncio.Task | None = None
    read_only_reason: str | None = None
    owner_external: bool = False

    @property
    def permission_mode(self):
        return {"readOnly": "read-only", "workspaceWrite": "workspace-write", "dangerFullAccess": "danger-full-access"}.get(self.sandbox.get("type"), "custom")

    async def disconnect(self):
        if self.mirror_task:
            self.mirror_task.cancel()
            await asyncio.gather(self.mirror_task, return_exceptions=True)
            self.mirror_task = None
        # Idle eviction must never stop the shared Codex process or another turn.
        if self.writable and self.turn_id is None:
            try:
                await self.bridge.client.call("thread/unsubscribe", {"threadId": self.session_id})
            except (CodexUnavailable, CodexRpcError):
                pass


class CodexBridge:
    def __init__(self, machine, client=None):
        self.machine = machine
        self.client = client or CodexClient(machine.cfg.codex_bin, machine.cfg.codex_timeout)
        self.available = False
        self.reason = "Codex 尚未连接" if machine.cfg.codex_enabled else "本机未启用 Codex"
        try:
            self.pins = SessionPinStore(machine.cfg.state_dir)
        except SessionPinStoreError:
            self.pins = None
        try:
            self.presentation = SessionPresentationStore(machine.cfg.state_dir)
        except SessionPresentationStoreError:
            self.presentation = None
        self.ask_tasks: set[asyncio.Task] = set()
        self.connect_lock = asyncio.Lock()
        self.catalog_cache: tuple[float, list[SessionInfo]] | None = None
        self.catalog_lock = asyncio.Lock()
        self.attach_lock = asyncio.Lock()
        self.lifecycle = NativeLifecycle()

    async def ensure(self):
        if not self.machine.cfg.codex_enabled:
            raise CodexUnavailable("本机未启用 Codex")
        async with self.connect_lock:
            await self.client.start(self.on_event, self.disconnected)
            account = await self.client.call("account/read", {})
            if account.get("requiresOpenaiAuth", True) and not account.get("account"):
                self.available = False
                self.reason = "请先在本机执行 codex login"
                raise CodexUnavailable(self.reason)
            self.available, self.reason = True, None

    async def start(self):
        if not self.machine.cfg.codex_enabled:
            return
        try:
            await self.ensure()
        except (CodexUnavailable, CodexRpcError, OSError):
            self.available = False
            if self.reason != "请先在本机执行 codex login":
                self.reason = "本机 Codex 未连接；请检查 CLI 与登录状态"

        await self.machine.transport.send(self.machine._engine_catalog())

    async def stop(self):
        for ctx in list(self.machine.sessions.values()):
            if ctx.engine == "codex" and ctx.sdk.mirror_task:
                ctx.sdk.mirror_task.cancel()
                await asyncio.gather(ctx.sdk.mirror_task, return_exceptions=True)
                ctx.sdk.mirror_task = None
        for task in list(self.ask_tasks):
            task.cancel()
        await asyncio.gather(*self.ask_tasks, return_exceptions=True)
        self.ask_tasks.clear()
        await self.client.stop()

    async def disconnected(self):
        self.available = False
        self.reason = "Codex 本机连接已断开，请重启连接后操作"
        for ctx in list(self.machine.sessions.values()):
            if ctx.engine != "codex":
                continue
            ctx.sdk.writable = False
            ctx.sdk.read_only_reason = "Codex 本机连接已断开；重新进入后核对状态，消息不会自动重发。"
            self.machine._cancel_pending_asks(ctx)
            await self.machine._emit(ctx, self.control(ctx, "Codex 连接已断开；当前消息不会自动重发"))
        await self.machine.transport.send(self.machine._engine_catalog())

    def control(self, ctx, reason=None):
        revision = self.machine._control_revision_epochs.get(ctx.key, 0)+1
        self.machine._control_revision_epochs[ctx.key] = revision
        ctx.control_revision = revision
        ctx.control_mode = "desktop" if ctx.sdk.owner_external else "remote"
        ctx.write_state = "writable" if ctx.sdk.writable else "read_only"
        ctx.control_reason = reason
        ctx.control_can_takeover = False
        return SessionControl(control_mode=ctx.control_mode,
                              write_state="writable" if ctx.sdk.writable else "read_only",
                              terminal_attached=False, reason=reason, can_takeover=False,
                              generation=self.machine.instance_id, revision=revision)

    async def error(self, cmd, message, code=ERR_INTERNAL):
        result = Error(code=code, message=message, request_id=getattr(cmd, "request_id", None) or getattr(cmd, "cmd_id", None),
                       msg_id=getattr(cmd, "msg_id", None), sid=getattr(cmd, "sid", None) or getattr(cmd, "session_id", None),
                       to=getattr(cmd, "client_id", None))
        await self.machine.transport.send(result)
        return result

    def owns(self, cmd):
        explicit_engine = getattr(cmd, "engine", None)
        if explicit_engine is not None:
            return explicit_engine == "codex"
        sid = getattr(cmd, "sid", None) or getattr(cmd, "session_id", None)
        ctx = self.machine._ctx_for(sid) if sid else None
        return getattr(cmd, "engine", None)=="codex" or bool(sid and sid.startswith("codex@")) or bool(ctx and ctx.engine=="codex")

    async def dispatch(self, cmd):
        from .command_router import UNHANDLED_COMMAND
        if not self.owns(cmd):
            return UNHANDLED_COMMAND
        methods = {
            "list_sessions": self.list_sessions, "new_session": self.new_session, "switch_session": self.switch_session,
            "get_history": self.history, "get_turn_detail": self.detail, "get_models": self.models,
            "get_history_image": self.image,
            "set_model": self.settings, "set_effort": self.settings, "set_perm": self.settings,
            "get_context": self.context, "answer_question": self.machine._handle_answer_question,
            "rename_session": self.mutate, "archive_session": self.mutate, "pin_session": self.mutate,
            "delete_session": self.mutate, "get_engine_capabilities": self.capabilities,
            "acknowledge_completion": self.acknowledge, "fork_session": self.fork,
        }
        handler = methods.get(cmd.type)
        if handler is None:
            return UNHANDLED_COMMAND
        try:
            if getattr(cmd, "space", "code") != "code":
                return await self.error(cmd, "当前仅支持 Code 会话", ERR_AUTH)
            await self.ensure()
            return await handler(cmd)
        except ValueError as exc:
            if cmd.type in {"get_history", "get_turn_detail"}:
                return await self.failed_read(cmd, str(exc))
            return await self.error(cmd, str(exc), ERR_BAD_PROMPT)
        except (CodexRpcError, CodexUnavailable, OSError) as exc:
            # Native errors can contain provider/account data. Correlated reads
            # must settle in their own response shape, not leave infinite spinners.
            message = "Codex 请求未完成，请检查本机连接或稍后重试"
            log.warning("Codex command failed: command=%s error_type=%s rpc_code=%s",
                        cmd.type, type(exc).__name__, getattr(exc, "code", None))
            if cmd.type in {"get_history", "get_turn_detail"}:
                return await self.failed_read(cmd, message)
            return await self.error(cmd, message, ERR_NOT_RUNNING)

    async def failed_read(self, cmd, message):
        fields={"session_id":cmd.session_id,"sid":cmd.session_id,"events":[],
                "authoritative":False,"error":message,"before":cmd.before,
                "revision":self.machine._history_revision(cmd.session_id),
                "to":getattr(cmd,"client_id",None)}
        if cmd.type=="get_history":
            response=History(**fields)
        else:
            response=TurnDetail(**fields,turn_id=cmd.turn_id)
        await self.machine.transport.send(response)
        return response

    async def catalog(self, force=False):
        async with self.catalog_lock:
            if not force and self.catalog_cache and time.monotonic()-self.catalog_cache[0]<2:
                return self.catalog_cache[1]
            rows = []
            pins = self.pins.ids("codex") if self.pins is not None else frozenset()
            for archived in [False, True]:
                cursor = None
                for _ in range(8):
                    result = await self.client.call("thread/list", {"limit": 100, "cursor": cursor,
                        "sortKey": "updated_at", "archived": archived, "useStateDbOnly": True,
                        "sourceKinds": ["cli", "vscode", "appServer", "unknown"]})
                    for thread in result.get("data", [])[:100]:
                        identity = str(thread.get("id", ""))
                        if not _ID.fullmatch(identity):
                            continue
                        sid = "codex@"+identity
                        ctx = self.machine.sessions.get(sid)
                        status = thread.get("status", {}).get("type", "notLoaded")
                        state = ctx.state if ctx else "running" if status=="active" else "idle"
                        fields = self.machine._session_presentation_fields("codex", sid)
                        rows.append(SessionInfo(session_id=sid, native_session_id=identity, engine="codex", space="code",
                            summary=str(thread.get("name") or thread.get("preview") or "Codex 会话")[:512],
                            first_prompt=str(thread.get("preview") or "")[:512], cwd=str(thread.get("cwd") or ""),
                            git_branch=(
                                (thread.get("gitInfo") or {}).get("branch")
                                if isinstance(thread.get("gitInfo"), dict)
                                else thread.get("gitBranch")
                            ),
                            last_modified=str(thread.get("updatedAt") or thread.get("createdAt") or 0),
                            tag="archived" if archived else None, pinned=sid in pins,
                            state=state, codex_status=status if status in {"notLoaded", "idle", "systemError", "active"} else None,
                            **fields))
                    cursor = result.get("nextCursor")
                    if not cursor:
                        break
            self.catalog_cache = time.monotonic(), rows
            return rows

    async def list_sessions(self, cmd):
        result = SessionList(engine="codex", space="code", sessions=await self.catalog(),
                             request_id=getattr(cmd, "cmd_id", None), to=getattr(cmd, "client_id", None))
        await self.machine.transport.send(result)
        return result

    async def attach(self, sid=None, cwd=None, model=None, effort=None):
        async with self.attach_lock:
            if sid and sid in self.machine.sessions:
                resident = self.machine.sessions[sid]
                if not resident.sdk.writable and resident.sdk.pending_query is None and resident.sdk.turn_id is None:
                    thread, active = await self.probe(sid)
                    resident.sdk.native_thread = thread
                    if not active:
                        try:
                            resumed = await self.client.call("thread/resume", {"threadId": native_id(sid), "excludeTurns": True})
                        except CodexRpcError as exc:
                            if not writer_conflict(exc): raise
                            resident.sdk.owner_external = True
                            resident.sdk.read_only_reason = DESKTOP_CONTROL
                            await self.machine._set_state(resident, "idle")
                            return resident
                        resident.sdk.writable = True
                        resident.sdk.owner_external = False
                        resident.sdk.read_only_reason = None
                        resident.sdk.model = resumed.get("model") or resident.sdk.model
                        resident.sdk.effort = resumed.get("reasoningEffort") or resident.sdk.effort
                        resident.sdk.sandbox = resumed.get("sandbox") or resident.sdk.sandbox
                        resident.sdk.approval = resumed.get("approvalPolicy") if isinstance(resumed.get("approvalPolicy"), str) else resident.sdk.approval
                        await self.machine._set_state(resident, "idle")
                return resident
            if len(self.machine.sessions)>=self.machine.cfg.max_concurrent_sessions:
                victim = next((c for c in self.machine.sessions.values() if c.key!=self.machine.focused_sid and not c.btw
                               and c.state=="idle" and not c.queued_queries and not self.machine._query_queue_task_active(c)), None)
                if victim:
                    await victim.sdk.disconnect()
                    self.machine.sessions.pop(victim.key, None)
                elif len(self.machine.sessions)>=self.machine.cfg.max_concurrent_sessions:
                    raise ValueError("会话池已满，请等待一个任务结束后重试")
            thread, external = await self.probe(sid) if sid else (None, False)
            running = external
            read_only_reason = DESKTOP_CONTROL if external else None
            # Reading a desktop-owned active thread does not grant write ownership.
            if external:
                result = {"thread": thread, "cwd": thread.get("cwd"), "model": thread.get("model"),
                          "reasoningEffort": thread.get("reasoningEffort")}
            else:
                params = {"threadId": native_id(sid), "excludeTurns": True} if sid else {}
                if cwd:
                    path = Path(cwd).expanduser().resolve()
                    if not path.is_dir():
                        raise ValueError("项目目录不存在")
                    params["cwd"] = str(path)
                if not sid:
                    params["dynamicTools"] = [USER_INPUT_TOOL]
                    catalog = (await self.client.call("model/list", {"includeHidden": False})).get("data", [])
                    known = {row.get("model") or row.get("id") for row in catalog}
                    if model and model not in known:
                        raise ValueError("当前 Codex 模型不可用，请选择本机支持的模型后重试。")
                    if not model:
                        configured = None
                        try:
                            configured = ((await self.client.call("config/read", {"includeLayers": False})).get("config") or {}).get("model")
                        except (CodexRpcError, CodexUnavailable):
                            pass
                        model = configured if configured in known else next(
                            (row.get("model") or row.get("id") for row in catalog if row.get("isDefault")), None)
                if model:
                    params["model"] = model
                try:
                    result = await self.client.call("thread/resume" if sid else "thread/start", params)
                except CodexRpcError as exc:
                    if not sid or not writer_conflict(exc): raise
                    # An idle desktop thread can still hold the native writer
                    # lock. Opening its history must succeed without claiming
                    # control or turning a lock conflict into a generic error.
                    external = True
                    read_only_reason = DESKTOP_CONTROL
                    result = {"thread": thread, "cwd": thread.get("cwd"), "model": thread.get("model"),
                              "reasoningEffort": thread.get("reasoningEffort")}
            thread = result["thread"]
            identity = str(thread["id"])
            if not _ID.fullmatch(identity):
                raise ValueError("Codex 返回了无效会话标识")
            sid = "codex@"+identity
            handle = CodexHandle(self, identity, result.get("model"), effort or result.get("reasoningEffort"),
                                 writable=not external)
            handle.native_thread = thread
            handle.read_only_reason = read_only_reason
            handle.owner_external = external
            handle.approval = result.get("approvalPolicy") if isinstance(result.get("approvalPolicy"), str) else "on-request"
            handle.sandbox = result.get("sandbox") or handle.sandbox
            ctx = SessionContext(session_id=identity, key=sid, sdk=handle, engine="codex", space="code",
                cwd=str(result.get("cwd") or thread.get("cwd") or Path.home()), state="running" if running else "idle",
                buffer=RingBuffer(self.machine.cfg.ring_max_events, self.machine.cfg.ring_max_bytes))
            self.machine.sessions[sid] = ctx
            return ctx

    async def probe(self, sid):
        thread, turns, _, proof = await self.read_turns(sid, limit=1)
        active = bool(thread.get("status", {}).get("type") == "active"
                      or (proof and proof.active)
                      or (turns and apply_lifecycle(turns[-1], proof).get("status") == "inProgress"))
        # An oversized/incompletely inspected native source cannot grant write
        # ownership. Keep the external thread readable and fail closed.
        if proof and not proof.complete:
            active = True
        return thread, active

    async def mirror(self, ctx):
        from cc_remote.protocol import GetHistory
        stamp = None
        while self.machine.sessions.get(ctx.key) is ctx and not ctx.sdk.writable:
            try:
                thread = ctx.sdk.native_thread or {}
                proof = await asyncio.to_thread(self.lifecycle.read, thread)
                if proof and proof.stamp != stamp:
                    stamp = proof.stamp
                    await self.machine._set_state(ctx, "running" if proof.active or not proof.complete else "idle")
                    await self.history(GetHistory(session_id=ctx.key, detail="summary", limit=8))
                await asyncio.sleep(1)
            except (CodexRpcError, CodexUnavailable, OSError):
                await asyncio.sleep(2)

    async def focus(self, ctx, cmd):
        self.machine.focused_sid = ctx.key
        control = self.control(ctx, None if ctx.sdk.writable else ctx.sdk.read_only_reason or
                               (DESKTOP_CONTROL if ctx.sdk.owner_external else "当前 Codex 发送结果待确认，消息不会自动重发。"))
        snap = Snapshot(cc_session_id=ctx.key, state=ctx.state, tail_text=ctx.buffer.latest_tail_text(), cwd=ctx.cwd,
                        generation=self.machine.instance_id, control=control)
        focus = SessionFocus(session_id=ctx.key, cwd=ctx.cwd, request_id=getattr(cmd, "request_id", None), to=getattr(cmd,"client_id",None))
        await self.machine._emit(ctx, snap)
        await self.machine._emit(ctx, focus)
        await self.announce(ctx)
        queue = self.machine._query_queue_state(ctx)
        queue.can_start_now = ctx.sdk.writable
        await self.machine._emit(ctx, queue)
        await self.machine._resend_pending_asks(ctx.key, getattr(cmd,"client_id",None))
        if ctx.sdk.owner_external and not ctx.sdk.writable and (ctx.sdk.mirror_task is None or ctx.sdk.mirror_task.done()):
            ctx.sdk.mirror_task = asyncio.create_task(self.mirror(ctx))
        return snap, focus

    async def switch_session(self, cmd):
        return await self.focus(await self.attach(cmd.session_id), cmd)

    async def new_session(self, cmd):
        ctx = await self.attach(cwd=cmd.cwd or str(Path.home()), model=cmd.model, effort=cmd.effort)
        responses = await self.focus(ctx, cmd)
        if cmd.prompt is not None or cmd.images or cmd.files:
            from cc_remote.protocol import Query
            await self.machine._handle_query(Query(sid=ctx.key, prompt=cmd.prompt or "", msg_id=cmd.msg_id,
                                                   images=cmd.images, files=cmd.files))
        self.catalog_cache = None
        await self.machine._invalidate_session_list("codex", "code")
        return responses

    async def announce(self, ctx):
        if ctx.sdk.model:
            await self.machine._emit(ctx, Model(model=ctx.sdk.model))
        await self.machine._emit(ctx, Effort(effort=ctx.sdk.effort or "model-default"))
        await self.machine._emit(ctx, Perm(mode=ctx.sdk.permission_mode, options=PERMISSIONS))

    async def bind(self, ctx, turn):
        handle = ctx.sdk
        identity = str(turn["id"])
        if identity in handle.turn_messages:
            return
        handle.turn_id = identity
        handle.completion = asyncio.Event()
        handle.streamed.clear(); handle.started_items.clear(); handle.closed_items.clear()
        query = handle.pending_query
        msg_id = query.msg_id if query else identity
        handle.turn_messages[identity] = msg_id
        while len(handle.turn_messages)>512:
            handle.turn_messages.popitem(last=False)
        ctx.active_msg_id = msg_id
        if query:
            await self.machine._emit(ctx, UserMsg(msg_id=msg_id, client_msg_id=msg_id, prompt=query.prompt, images=query.images,
                files=[{"filename": f["filename"]} for f in query.files or []] or None))
        await self.machine._emit(ctx, TurnBinding(msg_id=msg_id, turn_id=identity))
        await self.machine._set_state(ctx, "interrupting" if handle.stop_requested else "running")
        if ctx.turn_task is None or ctx.turn_task.done():
            ctx.turn_task = asyncio.create_task(handle.completion.wait())
        handle.pending_query = None
        if handle.stop_requested:
            try:
                await self.client.call("turn/interrupt", {"threadId":ctx.session_id,"turnId":identity})
            except (CodexUnavailable, CodexRpcError):
                await self.machine._emit(ctx,Error(code=ERR_NOT_RUNNING,message="停止结果待确认，请等待状态更新"))

    async def start_turn(self, ctx, cmd):
        if not ctx.sdk.writable or not self.client.ready:
            return await self.error(cmd, "当前 Codex 会话不可写，请等待本机连接恢复", ERR_NOT_RUNNING)
        if ctx.state != "idle":
            return await self.error(cmd, "当前会话正在执行，请排队发送", ERR_BUSY)
        if not cmd.prompt.strip() and not cmd.images and not cmd.files:
            return await self.error(cmd, "消息内容为空", ERR_BAD_PROMPT)
        if validate_attachments(cmd.images, cmd.files):
            return await self.error(cmd, "附件不符合大小或格式限制", ERR_BAD_PROMPT)
        if cmd.files:
            # The app-server input contract supports images and local-image
            # paths. General files need a separate workspace upload contract.
            return await self.error(cmd, "Codex 当前支持文字和图片；请通过项目文件引用其他附件", ERR_BAD_PROMPT)
        inputs = [{"type": "text", "text": cmd.prompt}]
        inputs += [{"type": "image", "url": f"data:{image['media_type']};base64,{image['data']}"} for image in cmd.images or []]
        params = {"threadId": ctx.session_id, "input": inputs, "clientUserMessageId": cmd.msg_id,
                  "approvalPolicy": ctx.sdk.approval, "sandboxPolicy": ctx.sdk.sandbox}
        if ctx.sdk.model: params["model"] = ctx.sdk.model
        if ctx.sdk.effort: params["effort"] = ctx.sdk.effort
        ctx.sdk.pending_query = cmd
        ctx.sdk.stop_requested = False
        await self.machine._set_state(ctx, "running")
        try:
            result = await self.client.call("turn/start", params, rpc_id=cmd.msg_id)
            await self.bind(ctx, result["turn"])
        except CodexRpcError:
            ctx.sdk.pending_query = None
            ctx.sdk.stop_requested = False
            await self.machine._set_state(ctx, "idle")
            return await self.error(cmd, "Codex 未接受这条消息，请检查模型与权限设置", ERR_BAD_PROMPT)
        except CodexUnavailable:
            # The engine may have accepted this mutation. Remove the item from
            # Remote's FIFO, freeze writes, and reconcile through native history;
            # never let the queue drainer retry turn/start automatically.
            ctx.sdk.writable = False
            ctx.sdk.read_only_reason = "发送结果待确认；请重新打开会话核对，消息不会自动重发"
            await self.machine._emit(ctx, self.control(ctx, ctx.sdk.read_only_reason))
            return None
        return None

    async def interrupt(self, ctx, cmd):
        if not ctx.sdk.writable:
            return await self.error(cmd, "当前会话由其他 Codex 界面控制", ERR_AUTH)
        if ctx.state in {"interrupting", "draining"}:
            return None
        if not ctx.sdk.turn_id and ctx.sdk.pending_query is not None:
            ctx.sdk.stop_requested = True
            await self.machine._set_state(ctx, "interrupting")
            return None
        if not ctx.sdk.turn_id:
            return await self.error(cmd, "当前没有可停止的 Codex 任务", ERR_NOT_RUNNING)
        await self.machine._set_state(ctx, "interrupting")
        try:
            await self.client.call("turn/interrupt", {"threadId": ctx.session_id, "turnId": ctx.sdk.turn_id})
        except (CodexRpcError, CodexUnavailable):
            return await self.error(cmd, "停止结果尚未确认，请等待任务状态更新", ERR_NOT_RUNNING)
        self.machine._cancel_pending_asks(ctx)
        return None

    async def on_event(self, frame):
        method, params = frame.get("method"), frame.get("params") or {}
        if "id" in frame:
            if len(self.ask_tasks)>=32:
                await self.client.reject(frame["id"])
                return
            task = asyncio.create_task(self.interaction(frame))
            self.ask_tasks.add(task); task.add_done_callback(self.ask_tasks.discard)
            return
        sid = params.get("threadId")
        if not sid and isinstance(params.get("thread"), dict):
            sid = params["thread"].get("id")
        ctx = self.machine.sessions.get("codex@"+str(sid))
        if ctx is None or ctx.engine!="codex":
            return
        handle = ctx.sdk
        if method=="turn/started":
            await self.bind(ctx, params["turn"])
        elif method=="turn/completed":
            turn = params["turn"]
            if turn["id"] in handle.finished_turns: return
            handle.finished_turns[turn["id"]] = True
            if len(handle.finished_turns)>512: handle.finished_turns.popitem(last=False)
            if handle.turn_id and turn["id"]!=handle.turn_id:
                return
            if turn.get("status") == "failed":
                await self.machine._emit(ctx, Error(code=ERR_CC_CRASH, message=turn_failure(turn), msg_id=ctx.active_msg_id))
            await self.machine._emit(ctx, terminal_event(turn))
            handle.turn_id = None; handle.pending_query = None; handle.stop_requested = False; handle.completion.set()
            self.machine._cancel_pending_asks(ctx)
            await self.machine._set_state(ctx, "idle")
            self.catalog_cache = None
            await self.machine._invalidate_session_list("codex", "code")
        elif method in {"item/started", "item/completed"}:
            item=params.get("item", {}); identity=item.get("id")
            if item.get("type")=="userMessage":
                if not handle.pending_query and handle.turn_id not in handle.turn_messages:
                    for row in item_events(item): await self.machine._emit(ctx,row)
                return
            completed=method=="item/completed"
            if completed and identity in handle.closed_items: return
            events=item_events(item, completed=completed)
            if identity in handle.started_items:
                events=[e for e in events if e.type not in {"assistant_msg_start","tool_use"}]
            if completed and item.get("type")=="agentMessage":
                # Completed text is authoritative; append only the unstreamed
                # suffix, never duplicate a whole answer after streamed deltas.
                seen=handle.streamed.get(identity,0)
                events=[e.model_copy(update={"text":e.text[seen:]}) if isinstance(e,Delta) else e for e in events]
            for row in events: await self.machine._emit(ctx,row)
            if completed:
                handle.started_items.discard(identity)
                handle.streamed.pop(identity, None)
                handle.closed_items.add(identity)
                if len(handle.closed_items)>4096: handle.closed_items.pop()
            else:
                handle.started_items.add(identity)
                if len(handle.started_items)>4096: handle.started_items.pop()
        elif method=="item/agentMessage/delta":
            identity=params.get("itemId")
            delta=params.get("delta", "")
            if isinstance(identity,str) and isinstance(delta,str):
                if identity not in handle.streamed and len(handle.streamed)>=4096:
                    handle.streamed.pop(next(iter(handle.streamed)))
                handle.streamed[identity]=handle.streamed.get(identity,0)+len(delta)
                await self.machine._emit(ctx,Delta(message_id=identity,text=delta[:512*1024],channel="unknown"))
        elif method=="item/commandExecution/outputDelta":
            await self.machine._emit(ctx,ToolDelta(tool_use_id=params["itemId"],stream="output",delta=str(params.get("delta", ""))[:512*1024]))
        elif method=="thread/tokenUsage/updated":
            handle.token_usage=params.get("tokenUsage")
            await self.publish_context(ctx)
        # Authentication, configuration, plugin and unknown notifications are
        # intentionally not reflected into the relay.

    async def interaction(self, frame):
        identity, method, params = frame["id"], frame["method"], frame.get("params") or {}
        ctx=self.machine.sessions.get("codex@"+str(params.get("threadId")))
        if not ctx or not ctx.sdk.writable:
            await self.client.reject(identity); return
        try:
            if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                answer=await self.machine._on_ask(ctx, question=str(params.get("reason") or params.get("command") or "Agent 请求执行操作")[:65536],
                    header="Codex 权限确认",options=[{"label":"允许本次"},{"label":"拒绝"}],allow_text=False,multi_select=False)
                await self.client.respond(identity,{"decision":"accept" if answer=="允许本次" else "decline"})
            elif method=="item/tool/requestUserInput" or (
                method=="item/tool/call" and params.get("tool")==TOOL_NAME
            ):
                dynamic = method=="item/tool/call"
                arguments = params.get("arguments") if dynamic else params
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("invalid question arguments")
                questions = validated_questions(arguments.get("questions"))
                answers={}
                for question in questions:
                    options=[{"label":str(o.get("label", ""))[:512],"ds":str(o.get("description") or "")[:2048]} for o in question.get("options") or []][:5]
                    answer=await self.machine._on_ask(ctx, question=str(question.get("question", ""))[:65536],
                        header=str(question.get("header", ""))[:512],options=options if len(options)>=2 else [],
                        allow_text=dynamic or bool(question.get("isOther")) or len(options)<2,
                        secret=bool(question.get("isSecret")), multi_select=False)
                    answers[question["id"]]={"answers":[answer] if isinstance(answer,str) else answer}
                result = {"answers":answers}
                if dynamic:
                    result = {"success":True,"contentItems":[{"type":"inputText","text":json.dumps(result,ensure_ascii=False)}]}
                await self.client.respond(identity,result)
            else:
                await self.client.reject(identity)
        except BaseException:
            # Closing or interrupting a prompt cannot silently approve it.
            try:
                if method.endswith("requestApproval"):
                    await self.client.respond(identity,{"decision":"decline"})
                elif method=="item/tool/requestUserInput":
                    await self.client.respond(identity,{"answers":{}})
                elif method=="item/tool/call" and params.get("tool")==TOOL_NAME:
                    await self.client.respond(identity,{"success":False,"contentItems":[
                        {"type":"inputText","text":"User input was not completed. No approval was granted."}]})
                else:
                    await self.client.reject(identity)
            except (CodexUnavailable, CodexRpcError):
                pass

    async def read_turns(self, sid, *, before=None, limit=40, full=False):
        result=await self.client.call("thread/read",{"threadId":native_id(sid),"includeTurns":False})
        thread=result["thread"]
        proof = await asyncio.to_thread(self.lifecycle.read, thread)
        cursor=None; collected=[]; found=before is None; more=False
        for _ in range(64):
            page=await self.client.call("thread/turns/list",{
                "threadId":native_id(sid),"cursor":cursor,"limit":min(100,limit+1),
                "sortDirection":"desc","itemsView":"full" if full else "summary"})
            rows=page.get("data",[])
            for turn in rows:
                if not found:
                    if turn["id"]==before: found=True
                    continue
                collected.append(turn)
                if len(collected)>limit:
                    more=True;break
            cursor=page.get("nextCursor")
            if len(collected)>limit or not cursor: break
        else:
            raise ValueError("历史游标超出读取范围，请重新打开会话")
        if not found: raise ValueError("历史游标失效，请重新打开会话")
        return thread,[apply_lifecycle(t, proof) for t in reversed(collected[:limit])],more,proof

    async def history(self, cmd):
        ctx=self.machine.sessions.get(cmd.session_id)
        # Fence the read before any await: later streamed items must survive a
        # history snapshot whose native page was captured before those items.
        live_seq=ctx.seq if ctx else 0
        thread,turns,more,proof=await self.read_turns(cmd.session_id,before=cmd.before,limit=cmd.limit or 40,
                                                  full=cmd.detail=="full")
        handle=ctx.sdk if ctx else None
        if handle:
            handle.native_thread = thread
        events=[]; groups=[]
        for turn in turns:
            rows=history_events(turn,client_msg_id=handle.turn_messages.get(turn["id"]) if handle else None)
            if rows:
                groups.append((turn,rows))
        start=0;end=len(groups)
        for _,rows in groups: events+=rows
        projected=[row for row in materialize_history_turns(events,include_live_detail=True,include_reasoning=True)]
        for row in projected:
            # itemsView:summary deliberately excludes tools/reasoning. Keep a
            # truthful, count-free way to retrieve this turn's native full items.
            row["detailAvailable"] = True
        result=History(session_id=cmd.session_id,sid=cmd.session_id,revision=self.machine._history_revision(cmd.session_id),
            generation=self.machine.instance_id,build_seq=self.machine._history_build_sequences.get(cmd.session_id,0)+1,
            live_seq=live_seq, authoritative=True,detail=cmd.detail,
            events=events if cmd.detail=="full" else [],turns=projected if cmd.detail=="summary" else None,
            has_more=more,oldest_id=groups[start][0]["id"] if start<end else None,
            newest_id=groups[end-1][0]["id"] if end else None,before=cmd.before,
            in_progress=bool((handle and handle.writable and ctx.state!="idle") or (proof and proof.active)
                             or any(t.get("status")=="inProgress" for t in turns)),
            to=getattr(cmd,"client_id",None))
        self.machine._history_build_sequences[cmd.session_id]=result.build_seq
        await self.machine.transport.send(result)
        return result

    async def detail(self, cmd):
        ctx=self.machine.sessions.get(cmd.session_id)
        native_turn=next((native for native,visible in ctx.sdk.turn_messages.items() if visible==cmd.turn_id),cmd.turn_id) if ctx else cmd.turn_id
        # Fetch just this turn's full items; the sidebar/history path uses the
        # native lightweight summaries and does not scan entire transcripts.
        items=[];cursor=None
        for _ in range(32):
            page=await self.client.call("thread/items/list",{"threadId":native_id(cmd.session_id),
                "turnId":native_turn,"cursor":cursor,"limit":100,"sortDirection":"asc"})
            items += [entry.get("item",entry) for entry in page.get("data",[])];cursor=page.get("nextCursor")
            if not cursor: break
        if cursor: raise ValueError("该轮过程过大，请在本机查看完整内容")
        turn={"id":native_turn,"items":items,"status":"inProgress"}
        if not items: raise ValueError("该轮过程不存在或已过期")
        from .machine import _turn_detail_page
        events,more,oldest,newer,cursor=_turn_detail_page(history_events(turn),before=cmd.before,limit=getattr(cmd,"limit",192),max_bytes=2*1024*1024)
        result=TurnDetail(session_id=cmd.session_id,turn_id=cmd.turn_id,revision=self.machine._history_revision(cmd.session_id),
            events=events,before=cmd.before,has_more=more,oldest_cursor=oldest,has_newer=newer,newer_cursor=cursor,to=getattr(cmd,"client_id",None))
        await self.machine.transport.send(result); return result

    async def image(self, cmd):
        from .machine import _render_history_image
        import base64
        revision = self.machine._history_revision(cmd.session_id)
        response = HistoryImage(session_id=cmd.session_id, sid=cmd.session_id, turn_id=cmd.turn_id,
            image_id=cmd.image_id, variant=cmd.variant, request_id=cmd.request_id,
            revision=revision, to=getattr(cmd, "client_id", None))
        try:
            if cmd.revision and cmd.revision != revision:
                raise ValueError("会话历史已更新，请重新加载图片")
            ctx = self.machine.sessions.get(cmd.session_id)
            turn = next((native for native, visible in ctx.sdk.turn_messages.items() if visible==cmd.turn_id),cmd.turn_id) if ctx else cmd.turn_id
            cursor = None
            image = None
            for _ in range(32):
                page = await self.client.call("thread/items/list", {"threadId":native_id(cmd.session_id),
                    "turnId":turn,"cursor":cursor,"limit":32,"sortDirection":"asc"})
                for entry in page.get("data", []):
                    item = entry.get("item", entry)
                    if item.get("type") != "userMessage": continue
                    for index, candidate in enumerate(await asyncio.to_thread(user_images, item)):
                        if image_id(turn, index, candidate) == cmd.image_id:
                            image = candidate
                            break
                cursor = page.get("nextCursor")
                if image or not cursor: break
            if image is None:
                raise ValueError("历史图片不存在或已过期，请刷新会话")
            mime, width, height, data = await asyncio.to_thread(_render_history_image, image, cmd.variant)
            response.media_type, response.width, response.height = mime, width, height
            response.data = base64.b64encode(data).decode("ascii")
        except ValueError as exc:
            response.error = str(exc)
        except (CodexRpcError, CodexUnavailable, OSError):
            response.error = "历史图片暂时不可用，请稍后重试"
        await self.machine.transport.send(response)
        return response

    async def models(self, cmd):
        result=await self.client.call("model/list",{"includeHidden":False})
        rows=[{"id":m.get("model") or m["id"],"display_name":m.get("displayName") or m["id"],
               "description":str(m.get("description") or "")[:4096],"efforts":[e["reasoningEffort"] for e in m.get("supportedReasoningEfforts",[])],
               "is_default":bool(m.get("isDefault"))} for m in result.get("data",[])[:128]]
        default=next((m for m in result.get("data",[]) if m.get("isDefault")),{})
        config = {}
        try:
            config = (await self.client.call("config/read", {"includeLayers": False})).get("config") or {}
        except (CodexRpcError, CodexUnavailable):
            pass
        configured = config.get("model")
        chosen = next((m for m in result.get("data", []) if configured in {m.get("id"), m.get("model")}), default)
        supported = {e["reasoningEffort"] for e in chosen.get("supportedReasoningEfforts", [])}
        configured_effort = config.get("model_reasoning_effort")
        response=Models(engine="codex",models=rows,default_model=chosen.get("model") or chosen.get("id"),
            default_effort=configured_effort if configured_effort in supported else chosen.get("defaultReasoningEffort"),to=getattr(cmd,"client_id",None))
        await self.machine.transport.send(response); return response

    async def settings(self, cmd):
        ctx=self.machine._ctx_for(getattr(cmd,"sid",None))
        if not ctx or not ctx.sdk.writable: return await self.error(cmd,"当前会话不可调整设置",ERR_AUTH)
        # Model and reasoning effort are supplied on each turn/start request.
        # A running turn keeps its original settings; the next queued turn uses
        # these values, so the user can prepare a follow-up without interrupting.
        if ctx.state!="idle" and cmd.type not in {"set_model", "set_effort"}:
            return await self.error(cmd,"请等待当前任务结束后调整设置",ERR_BUSY)
        if cmd.type=="set_model":
            catalog=await self.client.call("model/list",{})
            model=next((m for m in catalog.get("data",[]) if cmd.model in {m.get("id"),m.get("model")}),None)
            if not model: raise ValueError("模型不在本机 Codex 目录中")
            ctx.sdk.model=model.get("model") or model["id"]
            efforts=[e["reasoningEffort"] for e in model.get("supportedReasoningEfforts",[])]
            if ctx.sdk.effort not in efforts: ctx.sdk.effort=model.get("defaultReasoningEffort")
        elif cmd.type=="set_effort":
            catalog=await self.client.call("model/list",{})
            model=next((m for m in catalog.get("data",[]) if ctx.sdk.model in {m.get("id"),m.get("model")}),None)
            if not model or cmd.effort not in [e["reasoningEffort"] for e in model.get("supportedReasoningEfforts",[])]:
                raise ValueError("该模型不支持这个思考强度")
            ctx.sdk.effort=cmd.effort
        else:
            if cmd.mode not in {p.id for p in PERMISSIONS}: raise ValueError("权限选项无效")
            ctx.sdk.sandbox={"type":{"read-only":"readOnly","workspace-write":"workspaceWrite","danger-full-access":"dangerFullAccess"}[cmd.mode]}
            if cmd.mode=="workspace-write":ctx.sdk.sandbox.update(writableRoots=[ctx.cwd],networkAccess=False,excludeTmpdirEnvVar=False,excludeSlashTmp=False)
            ctx.sdk.approval="never" if cmd.mode=="danger-full-access" else "on-request"
        await self.announce(ctx)

    async def publish_context(self, ctx):
        usage=ctx.sdk.token_usage or {};last=usage.get("last") or {};total=last.get("totalTokens") or 0;size=usage.get("modelContextWindow") or 0
        await self.machine._emit(ctx,ContextReport(total_tokens=total,max_tokens=size,percentage=(total/size*100) if size else 0,
                                                   model=ctx.sdk.model,available=bool(usage)))

    async def context(self, cmd):
        ctx=self.machine._ctx_for(getattr(cmd,"sid",None))
        if ctx: await self.publish_context(ctx)

    async def capabilities(self, cmd):
        result=EngineCapabilities(engine="codex",space="code",cwd=getattr(cmd,"cwd",None) or "",items=[],
            notes=["模型、认证和工具由本机 Codex 管理"],request_id=getattr(cmd,"cmd_id",None),to=getattr(cmd,"client_id",None))
        await self.machine.transport.send(result); return result

    async def acknowledge(self, cmd):
        if self.presentation is None: return None
        snapshot=await asyncio.to_thread(self.presentation.acknowledge_completion,"codex",cmd.sid,cmd.completion_id)
        result=self.machine._completion_state(snapshot);result.sid=cmd.sid
        await self.machine.transport.send(result);return result

    async def mutate(self, cmd):
        sid=cmd.session_id;identity=native_id(sid);ctx=self.machine.sessions.get(sid)
        if cmd.type in {"archive_session","delete_session"} and ctx and (ctx.state!="idle" or ctx.queued_queries):
            return await self.error(cmd,"请先停止任务并处理待发送消息",ERR_BUSY)
        if cmd.type=="rename_session":await self.client.call("thread/name/set",{"threadId":identity,"name":cmd.title})
        elif cmd.type=="archive_session":await self.client.call("thread/archive" if cmd.archived else "thread/unarchive",{"threadId":identity})
        elif cmd.type=="pin_session":
            if self.pins is None:raise ValueError("本机置顶状态不可写，请检查状态目录")
            await asyncio.to_thread(self.pins.set_pinned,"codex",sid,cmd.pinned)
        elif cmd.type=="delete_session":
            await self.client.call("thread/delete",{"threadId":identity})
            self.machine.sessions.pop(sid,None)
            if self.machine.focused_sid==sid:self.machine.focused_sid=None
        self.catalog_cache=None
        return await self.list_sessions(cmd)

    async def fork(self, cmd):
        result=await self.client.call("thread/fork",{"threadId":native_id(cmd.session_id),
            "lastTurnId":cmd.last_turn_id,"excludeTurns":True})
        thread=result["thread"]
        identity=str(thread["id"])
        if not _ID.fullmatch(identity):raise ValueError("Codex 返回了无效派生标识")
        child="codex@"+identity
        event=SessionForked(parent_session_id=cmd.session_id,session_id=child,
            cwd=str(result.get("cwd") or thread.get("cwd") or Path.home()),target="same_cwd",
            last_turn_id=cmd.last_turn_id,request_id=cmd.request_id,to=getattr(cmd,"client_id",None))
        self.catalog_cache=None
        await self.machine.transport.send(event)
        await self.machine._invalidate_session_list("codex","code")
        return event
