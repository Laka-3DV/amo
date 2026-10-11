"""Bounded loopback client for the DeepSeek Harness (DSH) Web API.

DSH owns its model credentials, plugin runtime, agents, and durable session log.
cc-remote talks only to DSH's local HTTP RPC carrier and its two downstream
WebSocket feeds.  The carrier currently has no authentication, so this module
deliberately refuses every non-loopback authority even when a caller supplies
one explicitly.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

from cc_remote.log import logger
from cc_remote.wrapper.dsh_auth import (
    dsh_credentials_hint,
    load_dsh_browser_secret,
    make_dsh_browser_cookie,
)
from cc_remote.wrapper.dsh_mux import (
    MuxStreamError,
    RemoteMuxClient,
)

log = logger("cc_remote.wrapper.dsh_client")

DSH_WIRE_PREFIX = "dsh@"
_COMMAND_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_COMMAND_LINE = re.compile(r"^/([a-z][a-z0-9_-]*)(?=$|[\t\n\r ])")

#: The DSH release this wrapper's RPC/event/journal contract is verified
#: against.  Reported to clients as one host fact; bump it together with the
#: adaptation work whenever the loopback DSH is upgraded.
DSH_ADAPTED_VERSION = "0.2.0-rc.2"

# DSH 0.1.2 moved the unary RPC carrier from dotted method paths
# (/api/session.list) to Typert namespace/method endpoints (/api/session/list)
# with the payload wrapped as {"args": {<param>: value}} where <param> is the
# Remote method's single wire parameter name.  cc-remote keeps the historical
# dotted method names as its stable internal vocabulary and maps them here.
# `request` is the overwhelmingly common parameter name; only list-like reads
# use `_request`.  Methods absent from this map are passed through unchanged
# (they either already match 0.1.2 or are being phased out and will 404).
_METHOD_TO_012: dict[str, tuple[str, str | None]] = {
    # (0.1.0 dotted name) -> (0.1.2 endpoint, single args field name)
    # args_field None means the Remote declares no parameters: send {} args.
    "session.list": ("session/list", "_request"),
    "session.create": ("session/create", "request"),
    # Registering the directory first is what puts a session under its folder in
    # the DSH UI: session/create takes workspaceId OR cwd, never both, and only
    # the workspace form is accounted into the sidebar's folder grouping.
    "workspace.create": ("workspace/create", "request"),
    "session.history": ("session/page", "request"),
    "session.models": ("session/modelCatalog", None),
    "session.selectModel": ("session/selectModel", "request"),
    "session.rename": ("session/rename", "request"),
    "session.fork": ("session/fork", "request"),
    "session.prompt": ("session/prompt", "request"),
    "session.attachment": ("session/attachment", "request"),
    "session.cancel": ("session/cancel", "request"),
    "workspace.archiveSession": ("workspace/archiveSession", "request"),
    "agentPreset.list": ("agentPresets/list", None),
    # Process-level permission presets (DSH 0.2.0).  Declares no parameters, so
    # the args object must stay empty rather than carrying a "request" field.
    "permissionPresets/catalog": ("permissionPresets/catalog", None),
    "skill.list": ("skills/list", "request"),
    # Typert command RPCs take flat args (agentId/line/images at top level).
    "commands/list": ("commands/list", "*"),
    "commands/execute": ("commands/execute", "*"),
    "account/getBalance": ("account/getBalance", "*"),
}


def dsh_wire_session_id(native_session_id: str) -> str:
    """Namespace one DSH id before it enters the shared cc-remote router."""
    if (
        not isinstance(native_session_id, str)
        or not native_session_id
        or len(native_session_id) > 123
        or not native_session_id[0].isalnum()
        or any(
            not (character.isalnum() or character in "._:-")
            for character in native_session_id
        )
    ):
        raise ValueError("DSH session id cannot be represented on the wire")
    return f"{DSH_WIRE_PREFIX}{native_session_id}"


def dsh_native_session_id(wire_session_id: str) -> str:
    """Decode an exact DSH wire id; bare UUIDs are intentionally rejected."""
    if not isinstance(wire_session_id, str) or not wire_session_id.startswith(
        DSH_WIRE_PREFIX
    ):
        raise ValueError("not a DSH wire session id")
    native = wire_session_id[len(DSH_WIRE_PREFIX):]
    if dsh_wire_session_id(native) != wire_session_id:
        raise ValueError("invalid DSH wire session id")
    return native


#: The SessionAddress union's subagent discriminator and accepted modes.
_SUBAGENT_MODES = frozenset({"one-shot", "continuable", "unknown"})


def dsh_address_from_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the DSH 0.2.0 address for one ``session.list`` row.

    DSH 0.2.0 refuses the plain ``{kind:"session"}`` address for a Session whose
    ``origin`` is ``subagent`` — ``session/page`` and ``session/follow`` both
    answer ``session/agent-busy: subagent Sessions require their durable parent
    address``.  A subagent child must be addressed through its durable parent
    plus the ``mode`` its subagent projection publishes.

    Returns ``None`` when the row cannot yield a usable address, so a caller
    keeps whatever it already had rather than guessing a parent.
    """
    session_id = row.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return None
    if row.get("origin") != "subagent":
        return {"kind": "session", "sessionId": session_id}
    parent = row.get("parentSessionId")
    if not isinstance(parent, str) or not parent:
        return None
    projections = row.get("projections")
    values = projections.get("values") if isinstance(projections, dict) else None
    identity = values.get("subagent") if isinstance(values, dict) else None
    mode = identity.get("mode") if isinstance(identity, dict) else None
    if mode not in _SUBAGENT_MODES:
        # DSH itself accepts "unknown"; guessing a concrete mode would claim a
        # lifecycle the row never published.
        mode = "unknown"
    return {
        "kind": "subagent",
        "parentSessionId": parent,
        "childSessionId": session_id,
        "mode": mode,
    }


def dsh_cursor_hint_from_row(row: Mapping[str, Any]) -> int | None:
    """Return the durable cursor ``session.list`` publishes for one row.

    A subagent Session's transient follow stream is the only other cursor
    source, and it is the one that can be unavailable; this projection cursor is
    the fallback.
    """
    projections = row.get("projections")
    if not isinstance(projections, dict):
        return None
    as_of = projections.get("asOfSeq")
    if not isinstance(as_of, int) or isinstance(as_of, bool) or as_of < 0:
        return None
    return as_of


class DshError(RuntimeError):
    """Base class for a DSH transport or contract failure."""


class DshUnavailable(DshError):
    """The configured loopback DSH host cannot currently be reached."""


class DshProtocolError(DshError):
    """DSH returned a carrier shape cc-remote cannot safely consume."""


class DshRpcError(DshError):
    """A well-formed DSH RPC returned a business error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True)
class DshPreflight:
    host: dict[str, Any]
    presets: list[dict[str, Any]]
    default_preset_id: str | None


@dataclass(frozen=True)
class DshCommandDescriptor:
    """Display-safe effective command contributed to one DSH Agent."""

    name: str
    description: str
    input_hint: str | None = None


@dataclass(frozen=True)
class DshCommandExecution:
    """Exact durable identity and settled result of one DSH command."""

    command_id: str
    kind: str
    text: str | None = None
    source_event_seq: int | None = None


FrameCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


def _consume_callback_exception(task: asyncio.Task[None]) -> None:
    """Sink a spawned event-callback task's exception into the log."""
    if task.cancelled():
        return
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.warning("DSH event callback failed", error=str(exc))
AvailabilityCallback = Callable[
    [bool, DshPreflight | None, str | None], Awaitable[None] | None
]


def normalize_dsh_url(value: str) -> str:
    """Return a canonical loopback HTTP origin or raise ``ValueError``."""
    raw = value.strip()
    if not raw:
        raise ValueError("DSH URL must not be empty")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("DSH URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "DSH URL must be a credential-free loopback http origin"
        )
    host = parsed.hostname
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != 80:
        authority = f"{authority}:{port}"
    return urlunsplit(("http", authority, "", "", ""))


# DSH 0.1.2 raises interactive waterfalls on the forwarded event stream; the
# machine consumes the pre-0.1.2 frame vocabulary, so translate the names.
_WATERFALL_TYPES = {
    "approval/request": "approval/requested",
    "user-questions/request": "question/requested",
}

#: streamId -> native session id for open session/follow streams.
_MUX_EVENTS_ENDPOINT = "$events"


class DshClient:
    """One supervised connection to a local DSH host.

    Unary traffic uses POST ``/api/<method>``.  DSH's two WebSockets are
    downlink-only; responding to approvals/questions goes through
    ``POST /api/respond`` and echoes the server request's rpcId.
    """

    ALLOWED_METHODS = frozenset({
        "host.describe",
        "session.list",
        "session.create",
        "session.history",
        "session.models",
        "session.selectModel",
        "session.rename",
        "session.fork",
        "session.prompt",
        "session.attachment",
        "session.cancel",
        "workspace.list",
        "workspace.create",
        "workspace.archiveSession",
        "agentPreset.list",
        "skill.list",
        "llm.models",
        # DSH 0.2.0 moved the selectable permission presets to this
        # process-level catalog; the Session projection now carries only the
        # current selection.
        "permissionPresets/catalog",
        # Plugin-contributed slash commands are discovered and executed
        # through DSH's Typert gateway.  Plugin installation/configuration RPCs
        # intentionally remain absent from this allowlist.
        "commands/list",
        "commands/execute",
        # Display-only wallet read. Login/profile/credential RPCs stay private.
        "account/getBalance",
    })
    PREFLIGHT_PRESET_CAP = 128
    #: Upper bound on remembered live-assistant attempts (see _remember_attempt).
    MAX_ASSISTANT_ATTEMPTS = 512

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        max_request_bytes: int = 32 * 1024 * 1024,
        max_response_bytes: int = 16 * 1024 * 1024,
        max_frame_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.base_url = normalize_dsh_url(base_url)
        parsed_timeout = float(timeout)
        if not math.isfinite(parsed_timeout) or not 0.1 <= parsed_timeout <= 60.0:
            raise ValueError("DSH timeout must be between 0.1 and 60 seconds")
        self.timeout = parsed_timeout
        self.max_request_bytes = max(1024, int(max_request_bytes))
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self.max_frame_bytes = max(1024, int(max_frame_bytes))
        # Constructing WrapperMachine is a pure operation in a large part of
        # the unit suite and in configuration probes.  Do not allocate an
        # AsyncClient until the first actual DSH request; otherwise every such
        # constructor owns a lifecycle resource even when DSH is never started.
        self._http: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.available = False
        self.preflight: DshPreflight | None = None
        self.unavailable_reason: str | None = None
        self._cookie: str | None = None
        # remote.mux event carrier state (DSH 0.1.2)
        self._mux: RemoteMuxClient | None = None
        self._events_stream: str | None = None
        self._control_stream: str | None = None
        self._follow_streams: dict[str, str] = {}
        # (native session id, attempt id) -> (turn, step) for the DSH 0.2.0
        # process-local assistant stream, whose chunk frames name only the
        # attempt.
        self._assistant_attempts: dict[tuple[str, str], tuple[int, int]] = {}
        # DSH 0.2.0 needs the parent-scoped address for a subagent Session, and
        # only the session roster knows it.  Remember what the roster published
        # so a later page/follow/cursor read addresses the session correctly.
        self._session_addresses: dict[str, dict[str, Any]] = {}
        self._session_cursor_hints: dict[str, int] = {}
        # DSH 0.2.0's selectable permission presets are a process-level catalog
        # rather than part of a Session projection; cache it once for every
        # session handle to mirror.
        self.permission_options: tuple[dict[str, Any], ...] = ()
        self._mux_on_mux: FrameCallback | None = None
        self._mux_on_host: FrameCallback | None = None
        # Forwarded-event client id from the $events ready frame; every
        # waterfall result must quote it.
        self._events_client_id: str | None = None

    async def call(
        self,
        method: str,
        payload: Mapping[str, Any] | None = None,
        *,
        rpc_id: str | None = None,
        no_timeout: bool = False,
    ) -> Any:
        if method not in self.ALLOWED_METHODS:
            raise ValueError(f"DSH RPC method is not allowlisted: {method}")
        request_id = rpc_id or uuid4().hex
        self._validate_rpc_id(request_id)
        wire_method, args_field, wire_payload = self._map_012_call(
            method, dict(payload or {}))
        args: dict[str, Any]
        if args_field is None:
            args = {}
        elif args_field == "*":
            # Flat args: the payload's keys ARE the Remote's wire parameters
            # (used by commands/list and commands/execute whose agentId/line
            # live at the args top level, not under a named request field).
            args = wire_payload
        else:
            args = {args_field: wire_payload}
        body = {
            "type": "client-request",
            "rpcId": request_id,
            "method": wire_method,
            "payload": {"args": args},
        }
        raw = self._encode_request(body)
        response = await self._post_bounded(
            f"/api/{wire_method}", raw, no_timeout=no_timeout,
        )
        if response.get("type") != "server-response":
            raise DshProtocolError("DSH RPC response has an invalid type")
        if response.get("rpcId") != request_id:
            raise DshProtocolError("DSH RPC response rpcId mismatch")
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            raise DshProtocolError("DSH RPC response has an invalid result")
        if result["ok"]:
            if "value" not in result:
                raise DshProtocolError("DSH successful RPC omitted value")
            return result["value"]
        error = result.get("error")
        if not isinstance(error, dict):
            raise DshProtocolError("DSH failed RPC omitted its error")
        code = error.get("code")
        message = error.get("message")
        details = error.get("details")
        if not isinstance(code, str) or not isinstance(message, str):
            raise DshProtocolError("DSH RPC error has an invalid shape")
        raise DshRpcError(
            code,
            message,
            details=details if isinstance(details, dict) else None,
        )

    @classmethod
    def _map_012_call(
        cls,
        method: str,
        payload: dict[str, Any],
    ) -> tuple[str, str | None, dict[str, Any]]:
        """Translate one historical dotted call to the DSH 0.1.2 wire shape.

        Returns ``(wire_method, args_field, wire_payload)``.  Payload fields
        are passed through unchanged: cc-remote already speaks DSH's camelCase
        field vocabulary; only the method path and the outer ``args`` wrapper
        are 0.1.2-specific.  ``args_field=None`` marks a parameterless Remote
        whose args object must stay empty.  ``payload=None`` becomes ``{}`` so
        list-like reads that historically sent no body still satisfy the args
        contract.
        """
        mapped = _METHOD_TO_012.get(method)
        if mapped is None:
            return method, "request", payload
        endpoint, args_field = mapped
        # Payload passes through untouched: cc-remote already sends DSH's
        # camelCase field vocabulary, and 0.1.2's strict Remote schemas are the
        # authoritative validator.  Any field mismatch surfaces as an explicit
        # gateway/arguments-invalid error that names the exact problem, so
        # per-endpoint field work belongs at the call sites, not here.
        return endpoint, args_field, payload

    async def decline_waterfall(self, event_id: str) -> bool:
        """Hand one forwarded event back to DSH's other answerers.

        ``{kind:"next"}`` means "this client does not handle it". If another
        answerer (the DSH UI) claims the request it is answered there; otherwise
        the waterfall falls through to its own fallback — fail-closed for
        approvals, "no answerer" for questions — which is strictly better than
        leaving the agent blocked on a phone prompt the user wants to dismiss.
        """
        try:
            return await self.answer_waterfall(event_id, {"kind": "next"})
        except DshUnavailable:
            raise
        except DshError as exc:
            log.warning(
                "DSH waterfall decline failed",
                event_id=event_id,
                error_type=type(exc).__name__,
            )
            return False

    async def answer_waterfall(
        self, event_id: str, outcome: Mapping[str, Any],
    ) -> bool:
        """Send one forwarded-event result through ``POST /api/$events/result``.

        DSH 0.1.2 replaced the old ``/api/respond`` client-response carrier with
        this RPC: the payload is ``{args: {clientId, eventId, outcome}}`` where
        clientId comes from the ``$events`` ready frame and outcome is one of
        ``{kind:"result", value}``, ``{kind:"next"}`` or ``{kind:"rejected",
        error}``.
        """
        self._validate_rpc_id(event_id)
        client_id = self._events_client_id
        if not isinstance(client_id, str) or not client_id:
            raise DshUnavailable(
                "DSH interactive event carrier is not ready"
            )
        body = {
            "type": "client-request",
            "rpcId": f"cc-remote-answer-{uuid4().hex[:16]}",
            "method": "$events/result",
            "payload": {"args": {
                "clientId": client_id,
                "eventId": event_id,
                "outcome": dict(outcome),
            }},
        }
        raw = self._encode_request(body)
        response = await self._post_bounded("/api/$events/result", raw)
        # The gateway answers with the ordinary server-response envelope.
        result = response.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            error = result.get("error") if isinstance(result, dict) else None
            message = (
                error.get("message") if isinstance(error, dict) else None
            )
            raise DshRpcError(
                "events/result-failed",
                str(message or "DSH rejected the interactive result"),
            )
        return True

    async def respond(
        self,
        rpc_id: str,
        value: Mapping[str, Any],
    ) -> bool:
        """Answer one DSH interactive request (approval or question batch).

        ``value`` keeps the pre-0.1.2 shape the machine builds
        (``{sessionId, approvalId, outcome}`` or ``{sessionId, answer:{answers}}``);
        it is projected onto the 0.1.2 outcome value here.
        """
        self._validate_rpc_id(rpc_id)
        payload = dict(value)
        answer = payload.get("answer")
        if isinstance(answer, dict) and "answers" in answer:
            # user-questions: the whole answer batch is the result value.
            outcome_value: Any = {"answers": answer["answers"]}
        else:
            # approval/requested: the machine sends the decided outcome string.
            outcome_value = payload.get("outcome")
        return await self.answer_waterfall(
            rpc_id, {"kind": "result", "value": outcome_value},
        )

    async def respond_error(
        self,
        rpc_id: str,
        *,
        code: str = "cancelled",
        message: str = "Remote interaction was cancelled",
    ) -> bool:
        """Close one interactive request without answering it.

        A cancelled or disconnected browser must never be implicit consent, and
        leaving the request pending blocks DSH's agent forever, so the result is
        rejected explicitly.
        """
        self._validate_rpc_id(rpc_id)
        return await self.answer_waterfall(rpc_id, {
            "kind": "rejected",
            "error": {
                "name": "RemoteInteractionCancelled",
                "message": message[:4096],
                "code": code,
            },
        })

    async def probe(self) -> DshPreflight:
        """Verify the minimum contract needed by the integration.

        DSH 0.1.2 dropped ``host.describe``; the host facts cc-remote presents
        (default provider/model) now come from the ``session/modelCatalog``
        Remote's ``default`` and the session roster supplies the current cwd
        when one is available.  ``session.list`` and ``agentPresets/list``
        keep their pre-0.1.2 meaning.
        """
        sessions = await self.call("session.list")
        presets = await self.call("agentPreset.list")
        catalog = await self.call("session.models")
        rows = sessions.get("items") if isinstance(sessions, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise DshProtocolError("session.list did not return an item list")
        # Warm the address/cursor cache from the same read that proves the
        # roster contract.
        for row in rows:
            self.remember_session_row(row)
        preset_rows = presets.get("presets") if isinstance(presets, dict) else None
        if (
            not isinstance(preset_rows, list)
            or not all(isinstance(row, dict) for row in preset_rows)
        ):
            raise DshProtocolError("agentPresets.list did not return presets")
        default_ids = [
            row.get("id") for row in preset_rows
            if row.get("isDefault") is True and isinstance(row.get("id"), str)
        ]
        if len(default_ids) > 1:
            raise DshProtocolError("agentPresets.list returned multiple defaults")

        # modelCatalog.default -> {provider, model, reasoningEffort}; this is
        # the DSH 0.1.2 replacement for the preflight host.model/provider.
        default_model: dict[str, Any] = {}
        if isinstance(catalog, dict):
            catalog_default = catalog.get("default")
            if isinstance(catalog_default, dict):
                default_model = catalog_default
        model_value = default_model.get("model")
        provider_value = default_model.get("provider")
        if not isinstance(model_value, str) or not model_value:
            raise DshProtocolError("session.modelCatalog omitted a default model")
        if not isinstance(provider_value, str) or not provider_value:
            raise DshProtocolError("session.modelCatalog omitted a default provider")

        # Preflight lives for the full supervised connection. Keep only the
        # path-free fields cc-remote actually presents instead of retaining a
        # second copy of the complete session catalog or arbitrary plugin data.
        newest_cwd: str | None = None
        for row in rows:
            candidate = row.get("cwd")
            if isinstance(candidate, str) and candidate:
                newest_cwd = candidate
                break
        host_projection: dict[str, Any] = {
            "version": DSH_ADAPTED_VERSION,
            "cwd": (newest_cwd or "")[:4096],
            "attachedSessions": len(rows),
            "canOpenPath": False,
            "provider": provider_value[:256],
            "model": model_value[:256],
        }

        preset_projection: list[dict[str, Any]] = []
        default_projection: dict[str, Any] | None = None
        for row in preset_rows:
            preset_id = row.get("id")
            # DSH 0.2.0 dropped the preset roster's ``trust`` field: a row is now
            # {id, isDefault, name?, description?, broken?}.  The wire protocol
            # still carries it (the client renders a cosmetic "user" badge), so
            # absent means "system" — never drop a usable preset over a label,
            # which is what filtering on the missing field used to do.
            trust = row.get("trust")
            if trust not in {"system", "user"}:
                trust = "system"
            is_default = row.get("isDefault")
            if (
                not isinstance(preset_id, str)
                or not preset_id
                or len(preset_id) > 256
                or not isinstance(is_default, bool)
            ):
                continue
            projected: dict[str, Any] = {
                "id": preset_id,
                "trust": trust,
                "isDefault": is_default,
            }
            name = row.get("name")
            description = row.get("description")
            broken = row.get("broken")
            if isinstance(name, str):
                projected["name"] = name[:256]
            if isinstance(description, str):
                projected["description"] = description[:4096]
            if isinstance(broken, str) and broken:
                projected["broken"] = broken[:4096]
            if len(preset_projection) < self.PREFLIGHT_PRESET_CAP:
                preset_projection.append(projected)
            if is_default:
                default_projection = projected

        representable_default = (
            default_ids[0]
            if default_ids
            and isinstance(default_ids[0], str)
            and 0 < len(default_ids[0]) <= 256
            else None
        )
        if (
            default_projection is not None
            and not any(
                row["id"] == default_projection["id"]
                for row in preset_projection
            )
        ):
            if len(preset_projection) >= self.PREFLIGHT_PRESET_CAP:
                preset_projection[-1] = default_projection
            else:
                preset_projection.append(default_projection)
        return DshPreflight(
            host=host_projection,
            presets=preset_projection,
            default_preset_id=representable_default,
        )

    async def start(
        self,
        on_mux: FrameCallback,
        on_host: FrameCallback,
        on_availability: AvailabilityCallback,
    ) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(
            self._supervise(on_mux, on_host, on_availability),
            name="cc-remote-dsh",
        )

    async def stop(self) -> None:
        self._stopping = True
        await self._mux_disconnect()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        client = self._http
        self._http = None
        if client is not None:
            await client.aclose()

    async def _supervise(
        self,
        on_mux: FrameCallback,
        on_host: FrameCallback,
        on_availability: AvailabilityCallback,
    ) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                preflight = await self.probe()
                await self._mux_connect(on_mux, on_host)
                self.preflight = preflight
                self.available = True
                self.unavailable_reason = None
                await self._invoke(on_availability, True, preflight, None)
                backoff = 1.0
                # Supervise loop waits on the mux carrier; a terminal read
                # error (socket close) returns here and triggers reconnect.
                mux = self._mux
                if mux is not None:
                    await mux.wait_closed()
                raise DshUnavailable("DSH event stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = self._safe_reason(exc)
                if self.available or reason != self.unavailable_reason:
                    log.info(
                        "DSH unavailable",
                        error_type=type(exc).__name__,
                        reason=reason,
                    )
                self.available = False
                self.preflight = None
                self.unavailable_reason = reason
                await self._invoke(on_availability, False, None, reason)
            finally:
                await self._mux_disconnect()
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2.0, 30.0)

    # ---- remote.mux event carrier (DSH 0.1.2) ----

    async def _mux_connect(
        self,
        on_mux: FrameCallback,
        on_host: FrameCallback,
    ) -> None:
        """Open the multiplexed event socket plus $events and control streams.

        DSH 0.1.2 multiplexes every Remote stream over one ``/api/remote.mux``
        socket.  ``$events`` replaces the old events.host feed (host-level
        session status/added/removed/error and approval/question waterfalls);
        ``session/control`` replaces the mux-side queue/job/projection frames.
        Per-resident ``session/follow`` streams are opened on demand through
        ``follow_session``/``unfollow_session`` and route their events into
        ``on_mux``.
        """
        url = self._websocket_url("/api/remote.mux")
        mux = RemoteMuxClient(
            url,
            self._dsh_cookie(),
            max_frame_bytes=self.max_frame_bytes,
            open_timeout=self.timeout,
        )
        await mux.connect()
        self._mux = mux
        self._mux_on_mux = on_mux
        self._mux_on_host = on_host

        events_ready = asyncio.Event()
        control_ready = asyncio.Event()

        def on_events_value(value: dict[str, Any]) -> None:
            if value.get("type") == "ready":
                client_id = value.get("clientId")
                if isinstance(client_id, str) and client_id:
                    # The forwarded-event client id correlates every waterfall
                    # result; $events/result requires it verbatim.
                    self._events_client_id = client_id
                events_ready.set()
                return
            self._adapt_events_value(value, on_host, on_mux)

        def on_control_value(value: dict[str, Any]) -> None:
            if value.get("type") == "baseline":
                control_ready.set()
            self._adapt_control_value(value, on_mux)

        def on_events_error(error: MuxStreamError) -> None:
            # A dead $events stream means approvals/questions can never reach
            # the browser (DSH blocks the agent meanwhile).  Log loudly and let
            # the supervisor rebuild the carrier.
            log.warning("DSH $events stream failed", error=str(error))

        def on_events_end() -> None:
            log.warning("DSH $events stream ended")

        def on_control_error(error: MuxStreamError) -> None:
            log.warning("DSH control stream failed", error=str(error))

        def on_control_end() -> None:
            log.warning("DSH control stream ended")

        self._events_stream = await mux.open(
            "$events", {"args": {}},
            on_value=on_events_value,
            on_error=on_events_error,
            on_end=on_events_end,
        )
        self._control_stream = await mux.open(
            "session/control", {"args": {}},
            on_value=on_control_value,
            on_error=on_control_error,
            on_end=on_control_end,
        )

        await asyncio.wait_for(
            asyncio.gather(events_ready.wait(), control_ready.wait()),
            timeout=self.timeout,
        )
        log.info(
            "DSH event carrier ready",
            events_client_id=self._events_client_id,
        )

    async def _mux_disconnect(self) -> None:
        mux = self._mux
        self._mux = None
        self._events_stream = None
        self._control_stream = None
        self._follow_streams = {}
        self._assistant_attempts = {}
        self._mux_on_mux = None
        self._mux_on_host = None
        if mux is not None:
            await mux.close()

    def remember_session_row(self, row: Mapping[str, Any]) -> None:
        """Cache the address and cursor one ``session.list`` row published."""
        session_id = row.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            return
        address = dsh_address_from_row(row)
        if address is not None:
            self._session_addresses[session_id] = address
        hint = dsh_cursor_hint_from_row(row)
        if hint is not None:
            self._session_cursor_hints[session_id] = hint

    def session_address(self, native_sid: str) -> dict[str, Any]:
        """Return the DSH address to use for one native session id."""
        cached = self._session_addresses.get(native_sid)
        if cached is not None:
            return dict(cached)
        return {"kind": "session", "sessionId": native_sid}

    def session_cursor_hint(self, native_sid: str) -> int | None:
        """Return the roster's durable cursor for one session, if published."""
        return self._session_cursor_hints.get(native_sid)

    async def refresh_permission_catalog(self) -> bool:
        """Read DSH's process-level permission preset catalog.

        DSH 0.2.0 ships the selectable presets here instead of inside the
        per-Session ``permissions`` projection, so this is the only source for a
        permission switcher.  Returns whether the option list changed.
        """
        value = await self.call("permissionPresets/catalog")
        options = DshSessionHandle._permission_catalog(value)
        if options == self.permission_options:
            return False
        self.permission_options = options
        return True

    async def follow_session(self, native_sid: str) -> None:
        """Open a session/follow stream for one resident session.

        DSH 0.2.0 stopped recording durable ``assistant/chunk`` journal events:
        live assistant presentation now arrives as *process-local* frames that a
        client must opt into with ``assistantStream: true``.  Without the flag a
        turn produces no incremental content at all, so the follow request
        always asks for it.
        """
        if self._mux is None or native_sid in self._follow_streams.values():
            return
        on_mux = self._mux_on_mux
        if on_mux is None:
            return
        mux = self._mux
        stream_id = await mux.open(
            "session/follow",
            {"args": {"request": {
                "address": self.session_address(native_sid),
                "assistantStream": True,
            }}},
            on_value=lambda value: self._adapt_follow_value(
                value, native_sid, on_mux),
            on_error=lambda error: self._follow_failed(native_sid, stream_id, error),
            on_end=lambda: self._follow_ended(native_sid, stream_id),
        )
        self._follow_streams[stream_id] = native_sid
        log.info("DSH follow opened", session_id=native_sid, stream_id=stream_id)

    async def fetch_session_cursor(self, native_sid: str) -> int | None:
        """Learn a session's current event cursor from a follow opening frame.

        ``session/page`` requires a ``throughSeq`` that DSH documents as "the
        inclusive log cut obtained from the corresponding follow opening frame",
        so the follow snapshot's cursor is the only authoritative source.  Open a
        follow, take the snapshot cursor, and close it again.

        The roster's ``projections.asOfSeq`` is a *lagging projection watermark*,
        not this cursor, so it is used only as a last resort when the follow is
        unavailable: it can bound a page too early and hide the newest messages,
        which is strictly better than failing the page outright but must never
        override a real opening-frame cursor.
        """
        owned = False
        mux = self._mux
        if mux is None:
            # No supervised carrier (e.g. a history page before start): open a
            # transient mux just for the cursor read and close it afterwards.
            mux = RemoteMuxClient(
                self._websocket_url("/api/remote.mux"),
                self._dsh_cookie(),
                max_frame_bytes=self.max_frame_bytes,
                open_timeout=self.timeout,
            )
            await mux.connect()
            owned = True
        cursor_holder: list[int | None] = [None]
        done = asyncio.Event()

        def on_value(value: dict[str, Any]) -> None:
            if value.get("type") == "snapshot":
                cursor = value.get("cursor")
                if isinstance(cursor, int):
                    cursor_holder[0] = cursor
                done.set()

        def on_error(_error: MuxStreamError) -> None:
            done.set()

        stream_id = await mux.open(
            "session/follow",
            {"args": {"request": {
                "address": self.session_address(native_sid),
            }}},
            on_value=on_value,
            on_error=on_error,
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            await mux.cancel(stream_id)
            if owned:
                await mux.close()
        if cursor_holder[0] is not None:
            return cursor_holder[0]
        return self.session_cursor_hint(native_sid)

    async def unfollow_session(self, native_sid: str) -> None:
        if self._mux is None:
            return
        for stream_id, sid in list(self._follow_streams.items()):
            if sid == native_sid:
                self._follow_streams.pop(stream_id, None)
                await self._mux.cancel(stream_id)

    def _follow_failed(
        self, native_sid: str, stream_id: str, error: MuxStreamError,
    ) -> None:
        log.warning(
            "DSH follow stream failed",
            session_id=native_sid,
            stream_id=stream_id,
            error=str(error),
        )
        self._follow_lost(native_sid, stream_id)

    def _follow_ended(self, native_sid: str, stream_id: str) -> None:
        log.info(
            "DSH follow stream ended",
            session_id=native_sid,
            stream_id=stream_id,
        )
        self._follow_lost(native_sid, stream_id)

    def _follow_lost(self, native_sid: str, stream_id: str) -> None:
        """Forget a dead follow stream and try to restore live delivery.

        The stream map is the idempotency key for ``follow_session``: leaving a
        dead entry behind would make every later open a no-op and silently drop
        the session's live turn content forever.  Reopen after a short delay so
        a permanently broken session cannot spin tightly.
        """
        self._follow_streams.pop(stream_id, None)
        if self._stopping or self._mux is None:
            return
        try:
            task = asyncio.create_task(
                self._reopen_follow_after_delay(native_sid),
                name=f"cc-remote-dsh-follow-restore-{native_sid[:24]}",
            )
        except RuntimeError:
            return
        task.add_done_callback(_consume_callback_exception)

    async def _reopen_follow_after_delay(self, native_sid: str) -> None:
        await asyncio.sleep(1.0)
        if self._stopping or self._mux is None:
            return
        if native_sid in self._follow_streams.values():
            return
        try:
            await self.follow_session(native_sid)
        except Exception as exc:
            log.warning(
                "DSH follow restore failed",
                session_id=native_sid,
                error_type=type(exc).__name__,
            )

    # ---- 0.1.0 -> 0.1.2 frame adaptation (machine-facing surface) ----

    def _adapt_events_value(
        self,
        value: dict[str, Any],
        on_host: FrameCallback,
        on_mux: FrameCallback,
    ) -> None:
        """Turn one $events frame into the legacy host/mux surface.

        Host-level session status frames go to ``on_host``; interactive
        waterfalls (approval/question) go to ``on_mux`` because that is where
        the machine's answerable-request handlers live.
        """
        frame_type = value.get("type")
        if frame_type == "emit":
            event = value.get("event")
            args = value.get("args")
            if not isinstance(event, str) or not isinstance(args, list):
                return
            payload: dict[str, Any] = {"type": event}
            # api-session/status emits (sessionId, running)
            if event == "api-session/status" and len(args) >= 2:
                payload["sessionId"] = args[0]
                payload["running"] = args[1] is True
            elif event in {"api-session/added", "api-session/removed",
                           "api-session/error", "api-session/activity"}:
                if args and isinstance(args[0], str):
                    payload["sessionId"] = args[0]
                if event == "api-session/error" and len(args) >= 2:
                    payload["message"] = args[1]
            self._emit_host(payload, on_host)
        elif frame_type == "waterfall":
            # DSH 0.1.2 raises interactive waterfalls on the forwarded event
            # stream.  The machine consumes the pre-0.1.2 frame vocabulary
            # (approval/requested, question/requested) keyed by an rpcId it can
            # answer, so translate names and carry eventId as that rpcId.
            event = value.get("event")
            event_id = value.get("eventId")
            request = value.get("request")
            agent_id = value.get("agentId")
            if not isinstance(event, str) or not isinstance(event_id, str):
                return
            wire_type = _WATERFALL_TYPES.get(event)
            log.info(
                "DSH waterfall received",
                event=event,
                event_id=event_id,
                agent_id=agent_id,
                mapped=wire_type,
            )
            if wire_type is None:
                # An unrecognized waterfall still must be answered, or DSH
                # blocks the agent forever: refuse it explicitly.
                self._decline_waterfall(event_id, event)
                return
            payload: dict[str, Any] = {"type": wire_type}
            if isinstance(agent_id, str) and agent_id:
                payload["sessionId"] = agent_id
            fields = request if isinstance(request, dict) else {}
            # approval/requested: approvalId/toolName/reason
            # question/requested: questions/header
            for key in ("approvalId", "toolName", "reason", "questions", "header"):
                if key in fields:
                    payload[key] = fields[key]
            # The machine answers approvals/questions from its mux lane.
            self._emit_mux_answered(payload, event_id, on_mux)
        # 'ready' and lifecycle frames have no host surface.

    def _emit_mux_answered(
        self, payload: dict[str, Any], rpc_id: str, on_mux: FrameCallback,
    ) -> None:
        """Emit a mux frame carrying the rpcId its answer must reference."""
        try:
            frame = {"rpcId": rpc_id, "payload": dict(payload)}
            result = on_mux(frame)
            if asyncio.iscoroutine(result):
                self._spawn_callback(result, "dsh mux event")
        except Exception as exc:
            log.warning("DSH mux event handler failed", error=str(exc))

    def _emit_host_answered(
        self, payload: dict[str, Any], rpc_id: str, on_host: FrameCallback,
    ) -> None:
        """Emit a host frame carrying the rpcId its answer must reference."""
        try:
            frame = {"rpcId": rpc_id, "payload": dict(payload)}
            result = on_host(frame)
            if asyncio.iscoroutine(result):
                self._spawn_callback(result, "dsh host event")
        except Exception as exc:
            log.warning("DSH host event handler failed", error=str(exc))

    def _decline_waterfall(self, event_id: str, event: str) -> None:
        """Refuse an unknown waterfall so DSH does not block the agent."""
        log.warning("declining unknown DSH waterfall", event=event)
        try:
            task = asyncio.create_task(
                self.answer_waterfall(
                    event_id, {"kind": "rejected", "error": {
                        "name": "UnsupportedRemoteWaterfall",
                        "message": f"cc-remote does not handle {event}",
                    }},
                ),
                name="cc-remote-dsh-waterfall-decline",
            )
        except RuntimeError:
            return
        task.add_done_callback(_consume_callback_exception)

    def _emit_host(self, payload: dict[str, Any], on_host: FrameCallback) -> None:
        try:
            frame = {"payload": dict(payload)}
            result = on_host(frame)
            if asyncio.iscoroutine(result):
                self._spawn_callback(result, "dsh host event")
        except Exception as exc:
            log.warning("DSH host event handler failed", error=str(exc))

    def _adapt_control_value(
        self, value: dict[str, Any], on_mux: FrameCallback
    ) -> None:
        """Turn one session/control frame into the legacy mux surface."""
        frame_type = value.get("type")
        if frame_type == "baseline":
            # The opening baseline carries each session's projection snapshot
            # as {sessionId: {asOfSeq, values: {key: value}}}.  Fan it out into
            # per-session projection frames so resident runtimes learn their
            # permissions/title/goal etc. without waiting for an update.
            baseline = value.get("value")
            projections = (
                baseline.get("projections")
                if isinstance(baseline, dict) else None
            )
            if isinstance(projections, dict):
                for session_id, entry in projections.items():
                    if not isinstance(session_id, str) or not isinstance(entry, dict):
                        continue
                    seq = entry.get("asOfSeq")
                    values = entry.get("values")
                    if not isinstance(seq, int) or isinstance(seq, bool):
                        continue
                    if not isinstance(values, dict):
                        continue
                    for key, projection_value in values.items():
                        if not isinstance(key, str) or not key:
                            continue
                        self._emit_mux({
                            "type": "session/projection",
                            "sessionId": session_id,
                            "key": key,
                            "value": projection_value,
                            "seq": seq,
                        }, on_mux)
            return
        if frame_type == "projection":
            # {sessionId, key, value, seq}
            session_id = value.get("sessionId")
            key = value.get("key")
            if not isinstance(session_id, str) or not isinstance(key, str):
                return
            self._emit_mux({
                "type": "session/projection",
                "sessionId": session_id,
                "key": key,
                "value": value.get("value"),
                "seq": value.get("seq", 0),
            }, on_mux)
        # queue/job lifecycle frames have no legacy mux surface.

    def _adapt_follow_value(
        self,
        value: dict[str, Any],
        native_sid: str,
        on_mux: FrameCallback,
    ) -> None:
        """Turn one session/follow item into the legacy mux event surface."""
        frame_type = value.get("type")
        if frame_type == "snapshot":
            cursor = value.get("cursor")
            if isinstance(cursor, int):
                self._emit_mux({
                    "type": "session/subscribed",
                    "sessionId": native_sid,
                    "lastSeq": cursor,
                }, on_mux)
            # The follow snapshot carries the session's full projection set
            # (permissions, context, title, goal, ...).  Fan it out so a
            # resident that opened follow AFTER the control baseline still
            # learns its projections.
            projections = value.get("projections")
            if isinstance(projections, dict):
                seq = projections.get("asOfSeq")
                values = projections.get("values")
                if isinstance(seq, int) and not isinstance(seq, bool):
                    if isinstance(values, dict):
                        for key, projection_value in values.items():
                            if not isinstance(key, str) or not key:
                                continue
                            self._emit_mux({
                                "type": "session/projection",
                                "sessionId": native_sid,
                                "key": key,
                                "value": projection_value,
                                "seq": seq,
                            }, on_mux)
            # A client that attaches mid-attempt gets the live stream's opening
            # revision here; the turn/step pair owns every later chunk frame,
            # which carries neither.
            baseline = value.get("assistantStream")
            attempt = baseline.get("activeAttempt") if isinstance(baseline, dict) else None
            if isinstance(attempt, dict):
                self._remember_attempt(native_sid, attempt)
            return
        if frame_type == "event":
            event = value.get("event")
            if not isinstance(event, dict):
                return
            event_type = str(event.get("type"))
            # Chunk-level events are far too numerous to log; the turn
            # boundaries prove whether live delivery reached this session.
            if not event_type.startswith("assistant/chunk"):
                log.info(
                    "DSH follow event",
                    session_id=native_sid,
                    type=event_type,
                    seq=event.get("seq"),
                )
            self._emit_mux({
                "type": "session/event",
                "sessionId": native_sid,
                "event": dict(event),
                "view": None,
            }, on_mux)
            return
        if frame_type == "assistant-stream":
            self._adapt_assistant_stream(value, native_sid, on_mux)
            return
        # 'error'/'end' for a follow are handled by stream-level callbacks.

    def _attempt_key(self, native_sid: str, attempt_id: Any) -> tuple[str, str] | None:
        if not isinstance(attempt_id, str) or not attempt_id:
            return None
        return (native_sid, attempt_id)

    def _remember_attempt(self, native_sid: str, frame: Mapping[str, Any]) -> None:
        """Record the turn/step that owns one live assistant attempt."""
        key = self._attempt_key(native_sid, frame.get("attemptId"))
        turn = frame.get("turn")
        step = frame.get("step")
        if key is None or not isinstance(turn, int) or isinstance(turn, bool):
            return
        if not isinstance(step, int) or isinstance(step, bool):
            return
        self._assistant_attempts[key] = (turn, step)
        # A stream that dies without its terminal frame would otherwise retain
        # one entry per aborted attempt for the life of the process.
        while len(self._assistant_attempts) > self.MAX_ASSISTANT_ATTEMPTS:
            self._assistant_attempts.pop(next(iter(self._assistant_attempts)))

    def _adapt_assistant_stream(
        self,
        value: dict[str, Any],
        native_sid: str,
        on_mux: FrameCallback,
    ) -> None:
        """Forward one process-local assistant stream frame.

        DSH 0.2.0 delivers live assistant presentation here instead of the
        removed durable ``assistant/chunk`` event.  ``start``/``end`` only
        bracket the attempt; ``chunk`` carries the payload the translator
        already knows how to render.  These frames are process-local and carry
        no Session seq, so they travel on their own mux surface and never touch
        the durable event watermark.
        """
        frame = value.get("frame")
        if not isinstance(frame, dict):
            return
        frame_type = frame.get("type")
        key = self._attempt_key(native_sid, frame.get("attemptId"))
        if frame_type == "start":
            self._remember_attempt(native_sid, frame)
            return
        if frame_type == "end":
            if key is not None:
                self._assistant_attempts.pop(key, None)
            return
        if frame_type != "chunk":
            return
        owned = self._assistant_attempts.get(key) if key is not None else None
        if owned is None:
            # The opening snapshot did not name this attempt (mid-stream
            # attach).  Incremental content is best-effort: dropping it keeps
            # the durable path authoritative rather than guessing a turn.
            log.debug(
                "DSH assistant stream chunk without an owning attempt",
                session_id=native_sid,
            )
            return
        turn, step = owned
        index = frame.get("index")
        self._emit_mux({
            "type": "session/assistant-stream",
            "sessionId": native_sid,
            "turn": turn,
            "step": step,
            "index": index if isinstance(index, int) else None,
            "time": frame.get("time"),
            "chunk": frame.get("chunk"),
        }, on_mux)

    def _emit_mux(self, payload: dict[str, Any], on_mux: FrameCallback) -> None:
        try:
            frame = {"payload": dict(payload)}
            result = on_mux(frame)
            if asyncio.iscoroutine(result):
                self._spawn_callback(result, "dsh mux event")
        except Exception as exc:
            log.warning("DSH mux event handler failed", error=str(exc))

    def _spawn_callback(self, coroutine: Any, name: str) -> None:
        try:
            task = asyncio.create_task(coroutine, name=f"cc-remote-{name}")
            task.add_done_callback(_consume_callback_exception)
        except RuntimeError:
            # Event loop is closing; drop the callback.
            pass

    def _websocket_url(self, path: str) -> str:
        parsed = urlsplit(self.base_url)
        return urlunsplit(("ws", parsed.netloc, path, "", ""))

    @staticmethod
    async def _invoke(callback: Callable[..., Any], *args: Any) -> None:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _validate_rpc_id(value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8", "surrogatepass")) > 256
            or "\x00" in value
        ):
            raise ValueError("DSH rpcId must be a bounded non-empty string")

    def _encode_request(self, value: Mapping[str, Any]) -> bytes:
        try:
            raw = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("DSH request is not JSON serializable") from exc
        if len(raw) > self.max_request_bytes:
            raise ValueError("DSH request exceeds the configured byte limit")
        return raw

    async def _post_bounded(
        self,
        path: str,
        raw: bytes,
        *,
        no_timeout: bool = False,
    ) -> dict[str, Any]:
        client = self._http_client()
        try:
            async with client.stream(
                "POST",
                path,
                content=raw,
                headers={"content-type": "application/json"},
                timeout=None if no_timeout else self.timeout,
            ) as response:
                if response.status_code != 200:
                    raise DshUnavailable(
                        f"DSH HTTP carrier returned {response.status_code}"
                    )
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError as exc:
                        raise DshProtocolError(
                            "DSH response has an invalid content-length"
                        ) from exc
                    if declared_size > self.max_response_bytes:
                        raise DshProtocolError("DSH response exceeds byte limit")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise DshProtocolError("DSH response exceeds byte limit")
                    chunks.append(chunk)
        except DshError:
            raise
        except (httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
            raise DshUnavailable("DSH loopback carrier is unavailable") from exc
        try:
            value = json.loads(b"".join(chunks))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DshProtocolError("DSH response is not valid JSON") from exc
        if not isinstance(value, dict):
            raise DshProtocolError("DSH response must be a JSON object")
        return value

    def _http_client(self) -> httpx.AsyncClient:
        client = self._http
        if client is None or client.is_closed:
            headers = {"content-type": "application/json"}
            cookie = self._dsh_cookie()
            if cookie is not None:
                headers["cookie"] = cookie
            client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.timeout),
                trust_env=False,
                follow_redirects=False,
            )
            self._http = client
        return client

    def _dsh_cookie(self) -> str | None:
        """Return the DSH 0.1.2 browser-session cookie, caching per secret.

        DSH's signing secret is stable across restarts (persisted under
        ~/.dsh/.credentials.yaml), so the minted cookie stays valid until its
        expiresAt.  Re-read the secret on failure so a rotated credential
        recovers without a wrapper restart.
        """
        if self._cookie is not None:
            return self._cookie
        secret = load_dsh_browser_secret()
        if secret is None:
            log.warning(
                "DSH browser credential unavailable; 401s expected",
                hint=dsh_credentials_hint(),
            )
            return None
        authority = self._cookie_authority()
        if authority is None:
            log.warning("DSH base URL has no loopback authority")
            return None
        cookie = make_dsh_browser_cookie(authority, secret)
        if cookie is None:
            return None
        self._cookie = cookie
        return cookie

    def _cookie_authority(self) -> str | None:
        """Extract the exact ``host[:port]`` the browser-trust fence expects.

        The fence derives the cookie authority from the Host header.  httpx
        sends base_url's host:port verbatim for a relative path request, so the
        authority is the base URL authority (defaulting the port by scheme).
        """
        try:
            parsed = urlsplit(self.base_url)
        except ValueError:
            return None
        host = parsed.hostname
        if host is None:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{port}"

    @staticmethod
    def _validate_server_request(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("type") != "server-request":
            raise DshProtocolError("DSH event envelope is not a server request")
        rpc_id = value.get("rpcId")
        method = value.get("method")
        payload = value.get("payload")
        if (
            not isinstance(rpc_id, str)
            or not rpc_id
            or len(rpc_id.encode("utf-8", "surrogatepass")) > 256
            or not isinstance(method, str)
            or not method
            or not isinstance(payload, dict)
            or payload.get("type") != method
        ):
            raise DshProtocolError("DSH event envelope has an invalid shape")
        return {
            "rpcId": rpc_id,
            "method": method,
            "payload": dict(payload),
        }

    @staticmethod
    def _safe_reason(exc: Exception) -> str:
        if isinstance(exc, DshRpcError):
            return f"{exc.code}: {exc.message}"[:384]
        if isinstance(exc, DshProtocolError):
            return str(exc)[:384]
        if isinstance(exc, DshUnavailable):
            return str(exc)[:384]
        return "DSH loopback service is unavailable"


class DshSessionHandle:
    """Small session-scoped facade used by ``SessionContext``.

    DSH owns the actual agent lifecycle in its host process; disconnecting a
    cc-remote resident therefore releases no DSH process and intentionally does
    not cancel a running turn.
    """

    def __init__(
        self,
        client: DshClient,
        session_id: str,
        cwd: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        agent_preset: str | None = None,
    ) -> None:
        self.client = client
        self.session_id = session_id
        self.cwd = cwd
        self.model = model
        self.effort = effort
        self.agent_preset = agent_preset
        # These are DSH session projections, not cc-remote defaults.  Keep them
        # unknown until the host supplies the selected Agent's actual values.
        self.permission_mode = ""
        self.permission_options: tuple[dict[str, Any], ...] = ()
        self.context_pressure: dict[str, int] = {}
        self.context_breakdown: dict[str, int] = {}
        self._projection_seq: dict[str, int] = {}

    async def disconnect(self) -> None:
        return None

    async def prompt(
        self,
        prompt: str,
        *,
        images: list[dict[str, Any]] | None = None,
        rpc_id: str,
        mode: str = "queue",
        time_zone: str | None = None,
    ) -> Any:
        if mode not in {"queue", "steer"}:
            raise ValueError("invalid DSH prompt mode")
        content: list[dict[str, Any]] = []
        if prompt:
            content.append({"type": "text", "text": prompt})
        for image in images or ():
            media_type = image.get("media_type")
            if media_type == "image/jpg":
                media_type = "image/jpeg"
            data = image.get("data")
            if media_type not in {
                "image/png", "image/jpeg", "image/webp", "image/gif",
            } or not isinstance(data, str):
                raise ValueError("invalid DSH image attachment")
            content.append({
                "type": "image",
                "mediaType": media_type,
                "data": data,
            })
        payload: dict[str, Any] = {
            "requestId": rpc_id,
            "sessionId": self.session_id,
            "mode": mode,
            "content": content,
        }
        if time_zone:
            payload["clientTimeZone"] = time_zone
        value = await self.client.call(
            "session.prompt", payload, rpc_id=rpc_id,
        )
        if not isinstance(value, dict) or value.get("accepted") is not True:
            raise DshProtocolError("session.prompt returned an invalid receipt")
        command = value.get("command")
        if command is not None and (
            not isinstance(command, dict)
            or command.get("kind") != "success"
            or (
                "text" in command
                and not isinstance(command.get("text"), str)
            )
        ):
            raise DshProtocolError("session.prompt returned an invalid command")
        return value

    async def resolve_command(
        self, line: str,
    ) -> DshCommandDescriptor | None:
        """Resolve an exact slash token against this Agent's live registry.

        Commands may be contributed or shadowed by the selected Agent Preset,
        so a static cc-remote list is never authoritative.  Non-command text
        returns without touching the host; unknown slash text remains on
        ``session.prompt`` so DSH can apply its Skill/pre-step semantics.
        """
        if not line.startswith("/"):
            return None
        match = _COMMAND_LINE.match(line)
        if match is None:
            return None
        name = match.group(1)
        value = await self.client.call("commands/list", {
            "agentId": self.session_id,
        })
        if not isinstance(value, list) or len(value) > 2048:
            raise DshProtocolError("commands/list returned an invalid catalog")
        resolved: DshCommandDescriptor | None = None
        seen: set[str] = set()
        for row in value:
            if not isinstance(row, dict):
                raise DshProtocolError("commands/list returned an invalid command")
            row_name = row.get("name")
            description = row.get("description")
            input_value = row.get("input")
            if (
                not isinstance(row_name, str)
                or not row_name
                or len(row_name) > 512
                or _COMMAND_NAME.fullmatch(row_name) is None
                or not isinstance(description, str)
                or len(description) > 16 * 1024
                or row_name in seen
                or (
                    input_value is not None
                    and (
                        not isinstance(input_value, dict)
                        or not isinstance(input_value.get("hint"), str)
                        or len(input_value["hint"]) > 4096
                    )
                )
            ):
                raise DshProtocolError("commands/list returned an invalid command")
            seen.add(row_name)
            if row_name == name:
                resolved = DshCommandDescriptor(
                    name=row_name,
                    description=description,
                    input_hint=(
                        input_value["hint"]
                        if isinstance(input_value, dict) else None
                    ),
                )
        return resolved

    async def execute_command(
        self,
        line: str,
        *,
        rpc_id: str,
    ) -> DshCommandExecution:
        """Execute one previously resolved command and retain its native id.

        DSH command handlers may legitimately run longer than the ordinary
        carrier health timeout.  The owning asyncio task is the cancellation
        boundary: cancelling it closes the HTTP request, which DSH forwards to
        the handler's AbortSignal.

        DSH 0.2.0 renamed this Remote's attachment parameter from ``images`` to
        ``submittedAttachments`` and validates the arg set exactly, so the old
        name is rejected with ``gateway/arguments-invalid`` before the handler
        ever runs.  A phone-side slash command carries no attachment, but the
        field is required and must be present as an empty list.
        """
        value = await self.client.call(
            "commands/execute",
            {
                "agentId": self.session_id,
                "line": line,
                "submittedAttachments": [],
            },
            rpc_id=rpc_id,
            no_timeout=True,
        )
        if not isinstance(value, dict):
            raise DshProtocolError("commands/execute omitted its execution")
        command_id = value.get("commandId")
        result = value.get("result")
        if (
            not isinstance(command_id, str)
            or not command_id
            or len(command_id.encode("utf-8", "surrogatepass")) > 1024
            or "\x00" in command_id
            or not isinstance(result, dict)
            or result.get("kind") not in {"success", "error"}
            or (
                result.get("kind") == "error"
                and not isinstance(result.get("text"), str)
            )
            or (
                "text" in result
                and not isinstance(result.get("text"), str)
            )
        ):
            raise DshProtocolError("commands/execute returned an invalid execution")
        source_seq = result.get("sourceEventSeq")
        if (
            source_seq is not None
            and (
                result["kind"] != "success"
                or not isinstance(source_seq, int)
                or isinstance(source_seq, bool)
                or source_seq < 0
            )
        ):
            raise DshProtocolError("commands/execute returned an invalid source event")
        return DshCommandExecution(
            command_id=command_id,
            kind=result["kind"],
            text=result.get("text"),
            source_event_seq=source_seq,
        )

    @staticmethod
    def _projection_integer(value: Any, *, field: str) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > 9_007_199_254_740_991
        ):
            raise DshProtocolError(
                f"DSH projection has an invalid {field} value"
            )
        return value

    @classmethod
    def _permission_options(
        cls, options: Any,
    ) -> tuple[dict[str, Any], ...]:
        """Normalize a selectable-preset list into wire permission options."""
        if not isinstance(options, list) or len(options) > 64:
            raise DshProtocolError("DSH permission options are invalid")
        projected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for option in options:
            if not isinstance(option, dict):
                raise DshProtocolError(
                    "DSH permission options have an invalid option"
                )
            option_id = option.get("value")
            name = option.get("name")
            description = option.get("description")
            if (
                not isinstance(option_id, str)
                or not 0 < len(option_id) <= 256
                or not isinstance(name, str)
                or not 0 < len(name) <= 256
                or option_id in seen
                or (
                    description is not None
                    and (
                        not isinstance(description, str)
                        or len(description) > 2048
                    )
                )
            ):
                raise DshProtocolError(
                    "DSH permission options have an invalid option"
                )
            seen.add(option_id)
            # ``custom`` is a derived display state in DSH and cannot be a
            # command target.  Preserve it as the current mode, never as a
            # selectable row.
            if option_id == "custom":
                continue
            display_name = (
                "Full Access"
                if option_id == "danger-full-access"
                else " ".join(
                    word[:1].upper() + word[1:]
                    for word in name.split("-")
                )
                if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                else name
            )
            projected.append({
                "id": option_id,
                "name": display_name,
                **(
                    {"description": description}
                    if description is not None else {}
                ),
                "danger": option_id == "danger-full-access",
            })
        return tuple(projected)

    @classmethod
    def _permission_projection(cls, value: Any) -> str:
        """Parse the ``permissions`` Session projection.

        DSH 0.2.0 narrowed this projection to the Session's current durable
        selection alone (``{currentValue}``): the selectable presets moved to
        the process-level ``permissionPresets/catalog`` Remote.  Requiring an
        ``options`` list here, as the 0.1.2 shape did, made every 0.2.0
        projection invalid — which is why the phone sat on "权限读取中" forever.
        """
        if not isinstance(value, dict):
            raise DshProtocolError("DSH permissions projection is invalid")
        current = value.get("currentValue")
        if not isinstance(current, str) or not 0 < len(current) <= 256:
            raise DshProtocolError("DSH permissions projection is invalid")
        return current

    @classmethod
    def _permission_catalog(cls, value: Any) -> tuple[dict[str, Any], ...]:
        """Parse ``permissionPresets/catalog`` into wire permission options."""
        if not isinstance(value, dict):
            raise DshProtocolError("DSH permission catalog is invalid")
        return cls._permission_options(value.get("options"))

    @classmethod
    def _model_selection_projection(
        cls, value: Any,
    ) -> tuple[str, str | None]:
        """Parse the 0.2.0 ``modelSelection`` projection.

        DSH 0.2.0 stopped publishing the per-session model route through
        ``session/modelCatalog`` (its ``default`` now carries only
        provider+model, with no ``reasoningEffort``).  The Session projection is
        the authoritative readout: ``{lastUsed, next}``, each
        ``{provider, model, reasoningEffort}``.  ``next`` is what the following
        turn will use, so it is what a composer control must show.
        """
        if not isinstance(value, dict):
            raise DshProtocolError("DSH modelSelection projection is invalid")
        selected = value.get("next")
        if not isinstance(selected, dict):
            selected = value.get("lastUsed")
        if not isinstance(selected, dict):
            raise DshProtocolError("DSH modelSelection projection is invalid")
        provider = selected.get("provider")
        model = selected.get("model")
        effort = selected.get("reasoningEffort")
        if not isinstance(provider, str) or not provider:
            raise DshProtocolError("DSH modelSelection omitted its provider")
        if not isinstance(model, str) or not model:
            raise DshProtocolError("DSH modelSelection omitted its model")
        from cc_remote.wrapper.dsh_stream import encode_dsh_model

        return (
            encode_dsh_model(provider, model),
            effort if isinstance(effort, str) and effort else None,
        )

    @classmethod
    def _context_pressure_projection(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            raise DshProtocolError("DSH contextPressure projection is invalid")
        projected: dict[str, int] = {}
        for field in ("pressureTokens", "projectedTokens", "contextWindow"):
            raw = value.get(field)
            if raw is not None:
                projected[field] = cls._projection_integer(raw, field=field)
        return projected

    @classmethod
    def _context_breakdown_projection(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            raise DshProtocolError("DSH contextBreakdown projection is invalid")
        projected: dict[str, int] = {}
        for field in ("systemTokens", "toolsTokens", "messageTokens"):
            raw = value.get(field)
            if raw is None:
                continue
            projected[field] = cls._projection_integer(raw, field=field)
        return projected

    def apply_projection(self, key: str, value: Any, seq: int) -> bool:
        """Apply one DSH last-wins projection without letting an older page
        overwrite a newer mux frame.  Returns whether the visible value changed.
        """
        if (
            not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < -1
            or seq > 9_007_199_254_740_991
        ):
            raise DshProtocolError("DSH projection has an invalid sequence")
        if key == "permissions":
            parsed: Any = self._permission_projection(value)
            current_value: Any = self.permission_mode
        elif key == "modelSelection":
            parsed = self._model_selection_projection(value)
            current_value = (self.model, self.effort)
        elif key == "contextPressure":
            parsed = self._context_pressure_projection(value)
            current_value = self.context_pressure
        elif key == "contextBreakdown":
            parsed = self._context_breakdown_projection(value)
            current_value = self.context_breakdown
        else:
            return False
        previous_seq = self._projection_seq.get(key, -2)
        if seq < previous_seq:
            return False
        if seq == previous_seq and parsed != current_value:
            raise DshProtocolError(
                f"DSH {key} projection conflicts at sequence {seq}"
            )
        self._projection_seq[key] = seq
        if parsed == current_value:
            return False
        if key == "permissions":
            self.permission_mode = parsed
        elif key == "modelSelection":
            self.model, self.effort = parsed
        elif key == "contextPressure":
            self.context_pressure = parsed
        else:
            self.context_breakdown = parsed
        return True

    def apply_permission_catalog(self, value: Any) -> bool:
        """Replace the selectable permission presets from the process catalog.

        DSH 0.2.0 removed the options from the per-Session projection, so this
        is the only source for the list a client may switch between.
        """
        options = self._permission_catalog(value)
        if options == self.permission_options:
            return False
        self.permission_options = options
        return True

    def apply_projections(
        self, values: Mapping[str, Any], seq: int,
    ) -> set[str]:
        changed: set[str] = set()
        for key in (
            "permissions", "modelSelection",
            "contextPressure", "contextBreakdown",
        ):
            if key in values and self.apply_projection(key, values[key], seq):
                changed.add(key)
        return changed

    async def refresh_projections(self) -> set[str]:
        """Report projection keys that the live carrier has already applied.

        DSH 0.1.2 stopped folding projections into history pages; every
        projection (permissions, context pressure/breakdown, title, goal, ...)
        now arrives on the session/control stream and on each session/follow
        snapshot, which the machine applies through ``apply_projection``.

        Reading a history page here would be both pointless and harmful: it
        cannot carry projections any more, and issuing an extra RPC per context
        read (plus a transient mux handshake when the carrier is not yet up)
        made ``get_context`` fail with a handshake timeout whenever DSH was
        busy.  Callers that need the authoritative values read the ones the
        stream already applied.
        """
        return set()

    async def set_permission_mode(self, mode: str, *, rpc_id: str) -> str:
        """Switch one live DSH session through its native /permission command.

        Permissions arrive as a projection on the control/follow streams
        (``apply_projection``), so the local value is authoritative whenever the
        carrier has delivered it.  The known-option check only runs when that
        projection is present: right after a reconnect the projection may still
        be in flight, and refusing the switch then would be a false failure -
        DSH itself validates the preset name.
        """
        if not isinstance(mode, str) or not 0 < len(mode) <= 256:
            raise ValueError("invalid DSH permission preset")
        await self.refresh_projections()
        allowed = {option["id"] for option in self.permission_options}
        if allowed and mode not in allowed:
            raise ValueError("DSH permission preset is not selectable")
        if self.permission_mode == mode:
            return mode
        receipt_error: DshError | None = None
        try:
            execution = await self.execute_command(
                f"/permission {mode}", rpc_id=rpc_id,
            )
        except (DshUnavailable, DshProtocolError) as exc:
            receipt_error = exc
        else:
            if execution.kind != "success":
                raise DshRpcError(
                    "permission-switch-failed",
                    execution.text or "DSH rejected the permission preset",
                )
        try:
            await self.refresh_projections()
        except DshError:
            if receipt_error is not None:
                raise receipt_error
            raise
        if self.permission_mode != mode:
            if receipt_error is not None:
                raise receipt_error
            raise DshProtocolError(
                "DSH permission projection did not confirm the selection"
            )
        return mode

    async def cancel(self) -> Any:
        value = await self.client.call(
            "session.cancel", {"sessionId": self.session_id}
        )
        if not isinstance(value, dict) or value.get("accepted") is not True:
            raise DshProtocolError("session.cancel returned an invalid receipt")
        return value

    async def interrupt(self) -> Any:
        return await self.cancel()

    async def refresh_models(self) -> dict[str, Any]:
        value = await self.client.call(
            "session.models", {"sessionId": self.session_id}
        )
        if not isinstance(value, dict):
            raise DshProtocolError("session.modelCatalog returned an invalid value")
        # DSH 0.1.2 modelCatalog reports the active selection as `default`
        # ({provider, model, reasoningEffort}); the old `current` key is gone.
        current = value.get("current")
        if not isinstance(current, dict):
            current = value.get("default")
        if not isinstance(current, dict):
            raise DshProtocolError(
                "session.modelCatalog omitted the active model"
            )
        provider = current.get("provider")
        model = current.get("model")
        effort = current.get("reasoningEffort")
        if not isinstance(provider, str) or not isinstance(model, str):
            raise DshProtocolError(
                "session.modelCatalog active model is invalid"
            )
        from cc_remote.wrapper.dsh_stream import encode_dsh_model

        self.model = encode_dsh_model(provider, model)
        self.effort = effort if isinstance(effort, str) and effort else None
        return value

    async def refresh_permission_catalog(self) -> bool:
        """Adopt DSH's process-level permission presets into this session.

        DSH 0.2.0 moved the selectable presets out of the Session's
        ``permissions`` projection into a process-level catalog Remote, so this
        is the only source for the switcher's options.  The client caches the
        catalog for the whole process; a session only mirrors it.
        """
        changed = await self.client.refresh_permission_catalog()
        self.permission_options = self.client.permission_options
        return changed

    async def select_model(
        self,
        provider: str,
        model: str,
        effort: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sessionId": self.session_id,
            "provider": provider,
            "model": model,
        }
        if effort:
            payload["reasoningEffort"] = effort
        try:
            value = await self.client.call("session.selectModel", payload)
        except (DshUnavailable, DshProtocolError) as receipt_error:
            # Model selection is durable session state.  A loopback carrier can
            # fail after DSH committed it, so reconcile the exact current route
            # before telling the browser the mutation failed.
            try:
                current_value = await self.client.call(
                    "session.models", {"sessionId": self.session_id}
                )
            except DshError:
                raise receipt_error
            current = (
                current_value.get("current")
                if isinstance(current_value, dict) else None
            )
            if not isinstance(current, dict) and isinstance(current_value, dict):
                # DSH 0.1.2 modelCatalog names the active selection `default`.
                current = current_value.get("default")
            if not (
                isinstance(current, dict)
                and current.get("provider") == provider
                and current.get("model") == model
                and (
                    effort is None
                    or current.get("reasoningEffort") == effort
                )
            ):
                raise receipt_error
            value = {"selected": current}
        if not isinstance(value, dict) or not isinstance(value.get("selected"), dict):
            raise DshProtocolError("session.selectModel returned an invalid value")
        selected = value["selected"]
        applied_provider = selected.get("provider")
        applied_model = selected.get("model")
        applied_effort = selected.get("reasoningEffort")
        if not isinstance(applied_provider, str) or not isinstance(applied_model, str):
            raise DshProtocolError("session.selectModel omitted selected route")
        from cc_remote.wrapper.dsh_stream import encode_dsh_model

        self.model = encode_dsh_model(applied_provider, applied_model)
        self.effort = (
            applied_effort
            if isinstance(applied_effort, str) and applied_effort else None
        )
        return selected

    async def set_model(self, model: str) -> str:
        from cc_remote.wrapper.dsh_stream import decode_dsh_model

        provider, native_model = decode_dsh_model(model)
        catalog = await self.refresh_models()
        selected_effort: str | None = None
        groups = catalog.get("groups")
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict) or group.get("id") != provider:
                    continue
                models = group.get("models")
                if not isinstance(models, list):
                    continue
                row = next((
                    item for item in models
                    if isinstance(item, dict)
                    and item.get("id") == native_model
                ), None)
                reasoning = row.get("reasoning") if isinstance(row, dict) else None
                efforts = (
                    reasoning.get("efforts")
                    if isinstance(reasoning, dict) else None
                )
                effort_ids = {
                    item.get("id") for item in efforts or ()
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                }
                if self.effort in effort_ids:
                    selected_effort = self.effort
                else:
                    default_effort = (
                        reasoning.get("defaultEffort")
                        if isinstance(reasoning, dict) else None
                    )
                    if isinstance(default_effort, str) and default_effort:
                        selected_effort = default_effort
                break
        await self.select_model(provider, native_model, selected_effort)
        return self.model or model

    async def set_effort(self, effort: str) -> str:
        from cc_remote.wrapper.dsh_stream import decode_dsh_model

        if not self.model:
            await self.refresh_models()
        provider, native_model = decode_dsh_model(self.model or "")
        await self.select_model(provider, native_model, effort)
        return self.effort or effort
