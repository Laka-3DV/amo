"""The wrapper brain: a pool of per-session contexts + per-turn consumers.

Each SessionContext owns one cc subprocess (SdkHandle) plus its conversation
state (ring buffer, seq, state machine, turn task, translator, pending asks,
emit lock). The machine holds a pool `dict[key, SessionContext]` and a
`focused_sid`; relay/transport are singletons multiplexed by the `sid` field.

Per ctx, the drain contract is unchanged from the single-session design: one
async turn task per query; its consumer always runs to the terminal
ResultMessage (normal `success` or interrupted `error_during_execution`) before
that ctx's state returns to idle. interrupt() sets state=interrupting and the
SAME consumer keeps iterating until the terminal ResultMessage (the drain). A
drain timeout force-reconnects that ctx's SDK.

Reader/queue split: a background task iterates the SDK's async generator
WITHOUT asyncio.wait_for — wrapping __anext__ in wait_for corrupts the generator
when the short poll times out. The turn reads from an asyncio.Queue instead,
and cancelling queue.get (for the drain timeout) is safe and corrupts nothing.

Multi-session model:
- Routing identity is `ctx.key` — the real sid once known, else a temp
  `tmp-<uuid>` for a brand-new session. It equals the pool dict key and is what
  every emit stamps as `sid`, so a pre-capture new session's frames route to the
  right client runtime instead of leaking into whatever is focused.
- Switching the viewed session is FOCUS ONLY (SessionFocus) — no disconnect, so
  the previously-viewed session's turn keeps streaming in the background.
- When a new session captures its real cc id mid-turn, that is a re-key
  (SessionRekey: rename tmp-key -> sid), NOT a focus change — else a background
  session's capture would steal the user's view.
- Concurrency cap `max_concurrent_sessions`: over the cap, evict an idle,
  non-focused session (tear down its subprocess; the client keeps its runtime
  and re-spawns on re-focus). Reject only if ALL resident sessions are running.
- Token-aware: focus switching never reconnects a resident session; resume
  (cold prompt cache = full context re-send) happens ONLY on first spawn or on
  re-focus-after-eviction. Raising the cap trades RAM for fewer cold re-sends —
  don't evict sessions you're actively bouncing between.
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import io
import json
import math
import os
import plistlib
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4
from typing import Optional

from cc_remote.attachments import (
    MAX_IMAGE_PIXELS,
    decode_attachment,
    image_dimensions,
    validate_attachments,
)
from cc_remote.config import WrapperConfig
from cc_remote.log import logger
from cc_remote.protocol import (
    ASK_OPTION_MAX_COUNT, MAX_QUERY_QUEUE_BYTES, MAX_QUERY_QUEUE_ITEMS,
    Error, Hello, Query, QueryQueueState, QueuedQueryDetail, QueuedQueryInfo,
    QueuedQueryUpdated,
    Interrupt, CommandAck, Model, Models, EngineCatalog, EngineInfo,
    EngineCapabilities, Effort, Fast,
    Perm, PermissionModeInfo,
    ContextReport, StatusReport, Notice,
    RateLimitUpdate,
    ConversationTurn, History, TurnDetail, HistoryImage,
    FilePreview, FilesList, FileEntry, DiffReport, FILE_PREVIEW_MAX_BYTES,
    HistoryInvalidated, AskUser, AskUserClosed,
    ReplayStart, ReplayEnd, Snapshot, StateEvent,
    CompletionState,
    State, SessionControl,
    UserMsg, TurnSteered,
    ToolUse, ToolResult, ProcessEvent, TurnPlan, TurnBinding, TurnEnd,
    TurnNotificationContext,
    TurnResult, is_downstream,
    is_reliable_command,
    DshPresetInfo, SessionInfo, SessionList, SessionListInvalidated,
    SessionActivity,
    SessionFocus,
    SessionRekey, SessionForked,
    ERR_BUSY, ERR_NOT_RUNNING, ERR_BAD_PROMPT, ERR_DRAIN_TIMEOUT,
    ERR_CC_CRASH, ERR_INTERNAL, ERR_INVALID_CWD, ERR_AUTH, ERR_PROTOCOL,
    ERR_FORK_RECONCILING, ERR_NOT_STEERABLE, ERR_STEER_UNKNOWN,
    ERR_QUEUE_FULL,
)
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.claude_questions import (
    AskCancelled,
    AskSkipped,
    AskSuperseded,
    AskTimeout,
    AskUnavailable,
)
from cc_remote.wrapper.session_ctx import (
    ActiveTurnBinding,
    SessionContext,
)
from cc_remote.wrapper.session_pins import SessionPinStore, SessionPinStoreError
from cc_remote.wrapper.session_presentation import (
    SessionPresentationSnapshot,
    SessionPresentationStoreError,
)
from cc_remote.wrapper.history_store import (
    HistoryIndexStore,
    HistorySourceFingerprint,
    MaterializedHistoryPage,
    history_image_from_events,
    history_source_extends,
    materialize_history_turns,
)
from cc_remote.wrapper.command_router import CommandRouter, UNHANDLED_COMMAND
from cc_remote.wrapper.git_diff import bounded_process_output, read_git_diff
from cc_remote.wrapper.transport import WrapperTransport
from cc_remote.wrapper.codex_bridge import CodexBridge
from cc_remote.wrapper.dsh_client import (
    DshClient,
    DshError,
    DshPreflight,
    DshProtocolError,
    DshRpcError,
    DshSessionHandle,
    DshUnavailable,
    dsh_native_session_id,
    dsh_wire_session_id,
)
from cc_remote.wrapper.dsh_history import DshHistory
from cc_remote.wrapper.dsh_forks import DshForkJournal, DshForkJournalError
from cc_remote.wrapper.dsh_state import (
    DshSessionPinStore,
    DshSessionPresentationStore,
)
from cc_remote.wrapper.dsh_stream import (
    DshEventError,
    DshStreamTranslator,
    decode_dsh_model,
    dsh_command_item_id,
    encode_dsh_model,
    parse_dsh_fork_point,
)
log = logger("cc_remote.wrapper.machine")

CLAUDE_PERMISSION_MODES = frozenset({
    "default", "acceptEdits", "plan", "auto", "bypassPermissions",
})
CODEX_PERMISSION_MODES = frozenset({"never", "on-request", "untrusted"})
CODEX_COLLABORATION_MODES = frozenset({"default", "plan"})
CODEX_FAST_SERVICE_TIERS = frozenset({"fast", "priority"})
CODEX_EFFORT_RESOLVE_TIMEOUT_SECONDS = 1.0
CODEX_EFFORT_RESOLVE_RETRY_SECONDS = 30.0
_CLAUDE_OPUS_5_1M_ALIASES = frozenset({
    "opus",
    "opus[1m]",
    "claude-opus-5",
    "claude-opus-5[1m]",
})


def _rebind_turn_detail_visible_id(
    rows: tuple[dict, ...] | list[dict],
    *,
    indexed_turn_id: str,
    visible_turn_id: str,
) -> list[dict]:
    """Bind one proven detail segment to its official visible id."""
    if not rows:
        return []
    result: list[dict] = []
    for row in rows:
        cloned = dict(row)
        if cloned.get("turn_id") == indexed_turn_id:
            cloned["turn_id"] = visible_turn_id
        result.append(cloned)
    return result



def _turn_detail_event_group(event: dict) -> str | None:
    """Return the display-block identity used for intra-turn pagination."""
    event_type = event.get("type")
    if event_type in {"assistant_msg_start", "delta", "assistant_msg_end"}:
        identity = event.get("message_id")
        return f"message:{identity}" if isinstance(identity, str) else None
    if event_type in {"tool_use", "tool_delta", "tool_result"}:
        identity = event.get("tool_use_id")
        return f"tool:{identity}" if isinstance(identity, str) else None
    if event_type in {"process", "turn_plan", "turn_diff"}:
        identity = event.get("item_id")
        return f"item:{identity}" if isinstance(identity, str) else None
    # Control/user/terminal frames reconstruct the turn envelope but do not
    # consume one visible process slot.
    return None


def _coalesce_turn_detail_group(rows: list[dict]) -> list[dict]:
    """Fold repeated live snapshots without changing the rendered block.

    A display block is the atomic pagination unit.  Some app-server activities
    emit thousands of snapshots for one item id; replaying every snapshot makes
    that single atomic group larger than the WebSocket frame even though the UI
    ultimately retains only the last merged block.
    """
    if not rows:
        return []
    event_types = {row.get("type") for row in rows}
    if event_types == {"process"}:
        merged = dict(rows[0])
        for row in rows[1:]:
            append_to = row.get("append_to")
            delta = row.get("delta")
            if isinstance(append_to, str) and isinstance(delta, str):
                field = "progress" if append_to == "progress" else append_to
                current = merged.get(field)
                merged[field] = (
                    (current if isinstance(current, str) else "") + delta
                )
            else:
                for key, value in row.items():
                    if value is not None and key not in {"append_to", "delta"}:
                        merged[key] = value
        merged["append_to"] = None
        merged["delta"] = None
        return [merged]
    if event_types == {"turn_plan"} or event_types == {"turn_diff"}:
        return [dict(rows[-1])]
    if event_types <= {
        "assistant_msg_start", "delta", "assistant_msg_end",
    }:
        start = next(
            (dict(row) for row in rows
             if row.get("type") == "assistant_msg_start"),
            None,
        )
        deltas = [
            row.get("text") for row in rows
            if row.get("type") == "delta"
            and isinstance(row.get("text"), str)
        ]
        end = next(
            (dict(row) for row in reversed(rows)
             if row.get("type") == "assistant_msg_end"),
            None,
        )
        result: list[dict] = []
        if start is not None:
            result.append(start)
        if deltas:
            template = next(
                dict(row) for row in rows if row.get("type") == "delta")
            template["text"] = "".join(deltas)
            result.append(template)
        if end is not None:
            result.append(end)
        return result
    if event_types <= {"tool_use", "tool_delta", "tool_result"}:
        result = []
        use = next(
            (dict(row) for row in rows if row.get("type") == "tool_use"),
            None,
        )
        if use is not None:
            result.append(use)
        deltas: dict[str, dict] = {}
        delta_order: list[str] = []
        for row in rows:
            if row.get("type") != "tool_delta":
                continue
            stream = row.get("stream")
            if not isinstance(stream, str):
                continue
            if stream not in deltas:
                deltas[stream] = dict(row)
                deltas[stream]["delta"] = ""
                delta_order.append(stream)
            value = row.get("delta")
            if isinstance(value, str):
                deltas[stream]["delta"] += value
        result.extend(deltas[stream] for stream in delta_order)
        completed = next(
            (dict(row) for row in reversed(rows)
             if row.get("type") == "tool_result"),
            None,
        )
        if completed is not None:
            result.append(completed)
        return result
    # Mixed legacy groups are uncommon and already bounded per event. Preserve
    # their exact order; the final budget guard below still prevents an
    # oversized frame.
    return [dict(row) for row in rows]


def _bound_turn_detail_group(rows: list[dict], max_bytes: int) -> list[dict]:
    """Last-resort field compaction for one otherwise oversized atomic group."""
    if len(json.dumps(
        rows, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")) <= max_bytes:
        return rows
    bounded = []
    for row in rows:
        item = dict(row)
        if item.get("type") == "tool_use":
            item["input"] = {
                "_cc_remote_notice": "工具输入过大，已在此详情页截断",
            }
        elif item.get("type") == "process" and item.get("input") is not None:
            item["input"] = {
                "_cc_remote_notice": "处理输入过大，已在此详情页截断",
            }
        for key in (
            "text", "delta", "content", "summary", "detail", "output",
            "diff", "progress", "command", "explanation",
        ):
            value = item.get(key)
            if isinstance(value, str) and len(value) > 256 * 1024:
                item[key] = value[:256 * 1024] + "\n…（内容过大，已截断）"
                item["truncated"] = True
        bounded.append(item)
    if len(json.dumps(
        bounded, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")) <= max_bytes:
        return bounded

    template = rows[-1]
    base = {
        key: template[key] for key in ("v", "ts", "sid", "seq")
        if key in template
    }
    message = "此处理项本身超过单帧上限，已在本页显式截断。"
    group = _turn_detail_event_group(template)
    if group and group.startswith("message:"):
        message_id = group.removeprefix("message:")
        return [
            {**base, "type": "assistant_msg_start",
             "message_id": message_id, "channel": "unknown"},
            {**base, "type": "delta", "message_id": message_id,
             "text": message, "channel": "unknown"},
            {**base, "type": "assistant_msg_end",
             "message_id": message_id, "channel": "unknown"},
        ]
    if group and group.startswith("tool:"):
        tool_id = group.removeprefix("tool:")
        return [
            {**base, "type": "tool_use", "message_id": tool_id,
             "tool_use_id": tool_id, "tool": "oversized_detail",
             "input": {"notice": message}},
            {**base, "type": "tool_result", "tool_use_id": tool_id,
             "content": message, "is_error": False, "truncated": True},
        ]
    item_id = (
        group.removeprefix("item:")
        if group and group.startswith("item:")
        else f"oversized-detail-{hashlib.sha256(message.encode()).hexdigest()[:16]}"
    )
    return [{
        **base,
        "type": "process",
        "item_id": item_id,
        "kind": "task",
        "phase": "snapshot",
        "status": "succeeded",
        "title": "处理项过大",
        "summary": message,
        "truncated": True,
    }]


def _turn_detail_page(
    rows: tuple[dict, ...] | list[dict],
    *,
    before: str | None,
    limit: int,
    max_bytes: int | None = None,
) -> tuple[list[dict], bool, str | None, bool, str | None]:
    """Select a source-ordered, block-complete detail window."""
    grouped_rows: dict[str, list[dict]] = {}
    for row in rows:
        group = _turn_detail_event_group(row)
        if group is not None:
            grouped_rows.setdefault(group, []).append(row)
    coalesced_groups = {
        group: _bound_turn_detail_group(
            _coalesce_turn_detail_group(group_rows),
            max_bytes or 8 * 1024 * 1024,
        )
        for group, group_rows in grouped_rows.items()
    }

    event_groups: list[str | None] = []
    wire_rows: list[dict] = []
    ordered_groups: list[str] = []
    seen: set[str] = set()
    group_bytes: dict[str, int] = {}
    envelope_bytes = 0
    emitted_groups: set[str] = set()
    source_rows: list[dict] = []
    for row in rows:
        group = _turn_detail_event_group(row)
        if group is None:
            source_rows.append(row)
        elif group not in emitted_groups:
            emitted_groups.add(group)
            source_rows.extend(coalesced_groups[group])
    for row in source_rows:
        group = _turn_detail_event_group(row)
        event_groups.append(group)
        wire_row = {
            **row,
            # Conversation images already have source-bound thumbnail/full
            # endpoints. Never put their base64 bodies back into a heavy detail
            # frame; the summary row retains canonical imageRefs in the browser.
            **({"images": None} if row.get("type") == "user_msg"
               and row.get("images") else {}),
        }
        wire_rows.append(wire_row)
        row_bytes = len(json.dumps(
            wire_row, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8"))
        if group is None:
            envelope_bytes += row_bytes
        else:
            group_bytes[group] = group_bytes.get(group, 0) + row_bytes
        if group is not None and group not in seen:
            seen.add(group)
            ordered_groups.append(group)

    total = len(ordered_groups)
    requested_end: int | None = None
    if before is not None:
        if not before.isascii() or not before.isdigit():
            raise ValueError("invalid turn detail cursor")
        requested_end = int(before)
        if requested_end < 0 or requested_end > total:
            raise ValueError("turn detail cursor is out of range")
    page_limit = max(1, limit)

    def page_start(end: int) -> int:
        start = end
        page_bytes = envelope_bytes
        while start > 0 and end - start < page_limit:
            candidate = ordered_groups[start - 1]
            candidate_bytes = group_bytes.get(candidate, 0)
            if (max_bytes is not None and start < end
                    and page_bytes + candidate_bytes > max_bytes):
                break
            start -= 1
            page_bytes += candidate_bytes
        return start

    # Derive one canonical page chain from newest to oldest. Byte-bounded pages
    # can contain fewer than ``limit`` groups, so adding ``limit`` to the current
    # cursor can skip a whole intermediate page. Only advertise exact adjacent
    # boundaries from this chain in either direction.
    boundaries = [total]
    while boundaries[-1] > 0:
        boundaries.append(page_start(boundaries[-1]))
    end = total if requested_end is None else requested_end
    try:
        boundary_index = boundaries.index(end)
    except ValueError as exc:
        raise ValueError("turn detail cursor is not a page boundary") from exc
    start = (
        boundaries[boundary_index + 1]
        if boundary_index + 1 < len(boundaries)
        else end
    )
    selected = set(ordered_groups[start:end])
    page = [
        dict(row) for row, group in zip(wire_rows, event_groups)
        if group is None or group in selected
    ]
    has_more = start > 0
    oldest_cursor = str(start) if has_more else None
    has_newer = boundary_index > 0
    newer_cursor = (
        str(boundaries[boundary_index - 1]) if has_newer else None
    )
    return page, has_more, oldest_cursor, has_newer, newer_cursor


def _render_history_image(
    image: dict,
    variant: str,
) -> tuple[str, int, int, bytes]:
    """Validate and optionally thumbnail one indexed historical image."""
    media_type = image.get("media_type")
    encoded = image.get("data")
    if not isinstance(media_type, str) or not isinstance(encoded, str):
        raise ValueError("历史图片数据无效")
    if validate_attachments([image], None) is not None:
        raise ValueError("历史图片超出安全限制")
    raw = decode_attachment(encoded)
    dimensions = image_dimensions(raw, media_type)
    if dimensions is None:
        raise ValueError("历史图片格式无效")
    width, height = dimensions
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("历史图片尺寸过大")
    normalized = "image/jpeg" if media_type == "image/jpg" else media_type
    if variant == "full":
        return normalized, width, height, raw

    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with Image.open(io.BytesIO(raw)) as source:
        if int(getattr(source, "n_frames", 1)) != 1:
            raise ValueError("动态图片暂不支持预览")
        source.load()
        source.thumbnail((360, 360), Image.Resampling.LANCZOS)
        if source.mode not in {"RGB", "RGBA"}:
            source = source.convert("RGBA" if "transparency" in source.info else "RGB")
        output = io.BytesIO()
        source.save(
            output,
            format="WEBP",
            lossless=normalized == "image/png",
            quality=82,
            method=4,
        )
        thumb = output.getvalue()
        if len(thumb) > 512 * 1024:
            source.thumbnail((240, 240), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            source.convert("RGB").save(
                output, format="WEBP", quality=70, method=4)
            thumb = output.getvalue()
        return "image/webp", source.width, source.height, thumb


def _normalized_tool_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _session_permission_mode(ctx: SessionContext) -> str:
    """Return the permission mode actually configured on this live engine."""
    if ctx.engine == "codex":
        return getattr(ctx.sdk, "approval", "never")
    default = "" if ctx.engine == "dsh" else "bypassPermissions"
    return getattr(ctx.sdk, "permission_mode", default)


_DSH_IMAGE_INPUT_UNSUPPORTED = "当前 DSH 模型不支持图片输入。"


def _dsh_prompt_rejection_message(
    error: DshRpcError,
    *,
    fallback: str,
) -> str:
    """Map stable DSH prompt failures onto authored user-facing copy."""
    if (
        error.code == "attachment-error"
        and error.details.get("reason") == "MODEL_DOES_NOT_SUPPORT_IMAGES"
    ):
        return _DSH_IMAGE_INPUT_UNSUPPORTED
    return error.message[:4096] or fallback


#: Markers of a pre-0.2.0 (format v0) session log that DSH's own migration
#: chain refuses to observe.  Retrying cannot help: the artifact is unchanged
#: and DSH's own UI cannot open it either, so the message must not invite a
#: retry the way a transient carrier failure does.
_UNREADABLE_DSH_LOG_MARKERS = (
    "source v0 artifact",
    "refuses this format v0",
    "format-v0-to-v1",
    "cannot safely transform",
    "does not close the prior turn",
)


def _dsh_history_error_message(error: BaseException) -> str:
    """Return an accurate user-facing reason for one failed history read."""
    message = str(getattr(error, "message", "") or error)
    lowered = message.lower()
    if any(marker.lower() in lowered for marker in _UNREADABLE_DSH_LOG_MARKERS):
        return (
            "这条会话的日志是旧格式（v0），当前 DeepSeek Harness 无法读取；"
            "DSH 自身的界面同样打不开它，重试无效。"
        )
    return "DeepSeek Harness 历史暂时不可用，请稍后重试"


def _session_permission_event(
    ctx: SessionContext, **envelope,
) -> Perm | None:
    """Build the live permission projection for one resident session.

    Claude/Codex keep their static choices in Web. DSH's choices belong to the
    selected Agent's plugin composition and therefore travel with the value.
    """
    mode = _session_permission_mode(ctx)
    if not isinstance(mode, str) or not mode:
        return None
    options = None
    if ctx.engine == "dsh":
        raw_options = getattr(ctx.sdk, "permission_options", ())
        options = [PermissionModeInfo.model_validate(option)
                   for option in raw_options]
    return Perm(mode=mode, options=options, **envelope)


def _session_model(ctx: SessionContext) -> Optional[str]:
    """Return the live engine's authoritative per-session model, if known."""
    value = getattr(ctx.sdk, "model", None) or ctx.announced_model
    return value.strip() if isinstance(value, str) and value.strip() else None


def _session_effort(ctx: SessionContext) -> Optional[str]:
    """Return the live engine's effective/display reasoning strength."""
    explicit = getattr(ctx.sdk, "effort", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    value = getattr(ctx.sdk, "display_effort", None) or ctx.announced_effort
    return value.strip() if isinstance(value, str) and value.strip() else None


def _history_control_rows(
    sid: str,
    ctx: Optional[SessionContext],
    *,
    fallback_model: Optional[str] = None,
    allow_fallback_model: bool = False,
) -> list[dict]:
    """Return the one authoritative model/effort pair for a newest page."""
    model = _session_model(ctx) if ctx is not None else None
    if (
        model is None
        and allow_fallback_model
        and isinstance(fallback_model, str)
        and fallback_model.strip()
    ):
        model = fallback_model.strip()
    effort = _session_effort(ctx) if ctx is not None else None
    rows: list[dict] = []
    if model:
        rows.append(Model(model=model, sid=sid).model_dump(mode="json"))
    if effort:
        rows.append(Effort(effort=effort, sid=sid).model_dump(mode="json"))
    return rows



def _replace_history_control_rows(
    rows: list[dict] | tuple[dict, ...],
    controls: list[dict],
) -> list[dict]:
    """Install one control row per kind before the narrative projection.

    A resident value replaces every transcript/cache copy of the same kind.
    When one side is unavailable (notably a cold, non-resident cache hit), keep
    only the newest source value instead of erasing the best known readout.
    """
    selected = {
        row.get("type"): dict(row)
        for row in rows
        if row.get("type") in {"model", "effort"}
    }
    selected.update({row.get("type"): dict(row) for row in controls})
    return [
        *(selected[kind] for kind in ("model", "effort")
          if kind in selected),
        *(dict(row) for row in rows
          if row.get("type") not in {"model", "effort"}),
    ]


class _ForkOutcomeUncertain(RuntimeError):
    """A persistent fork may have committed and must not be ACKed/replayed."""



class _SpawnFailure(Exception):
    """Synchronous new-session failure routed only to its initiating client."""

    def __init__(self, message: str, *, size: int = 0, mtime_ns: int = 0,
                 revision: Optional[str] = None):
        super().__init__(message)
        self.size = size
        self.mtime_ns = mtime_ns
        self.revision = revision


class WrapperMachine:
    # Bounded in-memory at-most-once window for reliable commands.
    PROCESSED_COMMAND_CAP = 512
    PROCESSED_COMMAND_BYTES = 16 * 1024 * 1024
    SESSION_ALIAS_CAP = 256
    SESSION_ALIAS_TTL = 7 * 24 * 3600
    SESSION_ALIAS_FILE_MAX_BYTES = 2 * 1024 * 1024
    SESSION_ALIAS_PROFILE_META_KEY = "__cc_remote_profile__"
    COMMAND_IDS_PER_CLIENT = 512
    COMMAND_CLIENTS = 64
    COMMAND_RESPONSE_BYTES = 24 * 1024 * 1024
    NOTIFICATION_TITLE_CAP = 512
    NOTIFICATION_TITLE_LENGTH = 120
    DSH_RECEIPT_RECONCILE_DELAYS = (0.0, 0.1, 0.4, 1.0)
    DSH_INTERACTION_TASK_CAP = 256
    #: DSH 0.2.0's session roster accepts no filter and carries every session's
    #: full projection set — ~2 MB and ~0.5 s for a few hundred sessions. One
    #: entry (sidebar refresh, then attach) triggers several reads back to back,
    #: so a very short window collapses that burst into one DSH round trip while
    #: keeping the roster fresh enough for a control decision.
    DSH_CATALOG_TTL_SECONDS = 1.5
    #: Bound on interactive requests held while their session is not resident.
    PENDING_WATERFALL_CAP = 16
    #: How long an interactive request may wait for its session to attach.
    #: Long enough for any real attach race, short enough that a prompt the user
    #: already answered elsewhere cannot come back hours or days later.
    HELD_WATERFALL_TTL = 300.0
    DSH_COMMAND_ALIAS_CAP = 4096
    HISTORY_REFRESH_MIN_INTERVAL_SECONDS = 1.0
    HISTORY_REFRESH_MAX_INTERVAL_SECONDS = 10.0
    MIRROR_LIMIT = 4        # lightweight moving head; older turns stay paged
    FORK_RECONCILE_DELAY = 2.0
    UNCERTAIN_FORK_CAP = 64
    SAFE_RETRY_COMMANDS = frozenset({
        "fork_session", "fork_session_worktree", "new_session",
    })
    PREVIEW_WRITE_TOOLS = frozenset({
        "Write", "Edit", "MultiEdit", "NotebookEdit",
        "write_file", "apply_patch", "bash",
    })




    def __init__(self, cfg: WrapperConfig, transport: WrapperTransport):
        self.cfg = cfg
        self.transport = transport
        self._command_router = CommandRouter(self)
        self.instance_id = uuid4().hex
        # A History token changes for every wrapper process and every local
        # destructive conversation mutation.  Browsers persist this token with
        # IndexedDB turns, so a fresh wrapper can never merge a pre-crash cache
        # over its authoritative transcript.
        self._history_revision_epochs: dict[str, int] = {}
        # SessionContext objects are evicted and recreated while this wrapper
        # generation stays alive. Keep each sid's control revision outside the
        # resident context so a rebuilt Snapshot cannot move backwards and be
        # rejected by a browser that still holds the previous control watermark.
        self._control_revision_epochs: dict[str, int] = {}
        # Pool of resident sessions, keyed by browser routing sid (or a
        # ``tmp-<uuid>`` key until a brand-new session captures its native id).
        self.sessions: dict[str, SessionContext] = {}
        self.focused_sid: Optional[str] = None  # pool key of the viewed session
        self.transport.on_connected = self._on_transport_connected
        # ---- DeepSeek Harness carrier (loopback-only HTTP RPC + two WS feeds) ----
        self._dsh_client: DshClient | None = (
            DshClient(cfg.dsh_url, timeout=cfg.dsh_timeout)
            if getattr(cfg, "dsh_url", "") else None
        )
        self._dsh_history: DshHistory | None = (
            DshHistory(self._dsh_client) if self._dsh_client is not None else None
        )
        self._dsh_available = False
        self._dsh_preflight: DshPreflight | None = None
        # Short single-flight cache for the expensive DSH roster read (see
        # DSH_CATALOG_TTL_SECONDS). Guarded by its own lock so concurrent
        # callers coalesce onto one DSH round trip instead of queueing several.
        self._dsh_catalog_cache: tuple[float, list[dict], set[str]] | None = None
        self._dsh_catalog_lock = asyncio.Lock()
        self._dsh_unavailable_reason: str | None = (
            None if self._dsh_client is not None else "未配置 DSH 本机服务"
        )
        # Answerable mux frames must never block the DSH stream reader. Each
        # batch runs in a bounded task and serializes its cards through ctx.ask_lock.
        self._dsh_interaction_tasks: set[asyncio.Task] = set()
        self._dsh_pending_request_asks: dict[tuple[str, str], set[str]] = {}
        self._dsh_pending_approvals: dict[tuple[str, str], str] = {}
        # A question is raised inside a tool call, and that call's result is the
        # only durable proof DSH gives us that it was answered (by this phone or
        # by the desktop) — the waterfall itself settles silently.
        self._dsh_last_tool_call: dict[str, str] = {}
        self._dsh_request_calls: dict[tuple[str, str], str] = {}
        # approval/asked audit id -> tool call id, so approval/decided can close
        # the prompt we are showing for that call.
        self._dsh_approval_calls: dict[tuple[str, str], str] = {}
        # Interactive waterfalls that arrived before their session was resident.
        self._pending_waterfalls: dict[str, list[dict]] = {}
        # Transcript mirror: sessions a client has opened (registered on
        # GetHistory), sid -> {"path", "size", "engine"}. The watcher polls each
        # file's SIZE and, when it grows without us having written it, mirrors
        # the append to clients.
        self._watch: dict[str, dict] = {}
        # Display metadata is populated by catalog reads and successful local
        # mutations. Keep it bounded and memory-only: transcripts remain the
        # authority, and a terminal event must never synchronously reread a
        # multi-gigabyte history merely to format an OS notification.
        self._notification_titles: OrderedDict[str, str] = OrderedDict()
        self._watch_task: Optional[asyncio.Task] = None
        # Newest-page History builds can race the watcher, another client, and
        # live stream events. Sequence them per session so browsers can discard
        # an older build that completes after a newer one.
        self._history_build_sequences: dict[str, int] = {}
        self._history_command_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._history_page_tasks: dict[
            tuple[str, str, int, str], asyncio.Task
        ] = {}
        self._history_refresh_tasks: dict[
            tuple[str, str, int, str], asyncio.Task
        ] = {}
        self._history_refresh_dirty: set[tuple[str, str, int, str]] = set()
        # Rebuildable local projection of already-translated transcript pages.
        # Raw transcripts remain authoritative; exact source fingerprints make
        # the derived SQLite row a safe fast path rather than another history.
        try:
            self._history_index: HistoryIndexStore | None = HistoryIndexStore(
                Path(cfg.state_dir))
        except Exception as exc:
            self._history_index = None
            log.warning("history index unavailable", error=str(exc))
        # In-memory at-most-once window for client retries. The outer and inner
        # OrderedDicts are both bounded; wrapper process restart intentionally
        # resets this window (documented residual risk, not durable exactly-once).
        self._processed_commands: OrderedDict[
            str, OrderedDict[str, tuple[object, ...]]
        ] = OrderedDict()
        self._processed_command_sizes: dict[tuple[str, str], int] = {}
        self._processed_command_bytes = 0
        # Deferred browser queries are owned by resident SessionContexts, while
        # these machine-wide counters prevent many clients/sessions from each
        # allocating the full advertised queue allowance.
        self._queued_query_count = 0
        self._queued_query_bytes = 0
        # Durable temp-key -> real-id recovery. SessionRekey is a control frame;
        # if its one live send is lost after NewSession was ACKed, the next Hello
        # uses this map to replay the rekey before cursor catch-up.
        self._session_alias_profile_revision = 0
        self._session_aliases = self._load_session_aliases()
        try:
            self._dsh_forks: DshForkJournal | None = DshForkJournal(
                self.cfg.state_dir
            )
        except DshForkJournalError:
            # DSH itself remains usable, but an unreadable at-most-once ledger
            # must fail closed for persistent forks.
            self._dsh_forks = None
            log.exception("DSH fork journal unavailable")
        self._dsh_fork_locks: dict[str, asyncio.Lock] = {}
        # DSH 0.1.2 dropped workspace.list; the archived roster is cached from
        # workspace/archiveSession receipts (server returns the full list) and
        # refreshed by workspace/follow events when that stream is connected.
        self._dsh_archived_session_ids: set[str] = set()
        try:
            self._dsh_session_pins: DshSessionPinStore | None = (
                DshSessionPinStore(self.cfg.state_dir)
            )
        except SessionPinStoreError:
            self._dsh_session_pins = None
            log.exception("DSH session pin store unavailable")
        try:
            self._dsh_session_presentation = DshSessionPresentationStore(
                self.cfg.state_dir
            )
        except SessionPresentationStoreError:
            self._dsh_session_presentation = None
            log.exception("DSH session presentation store unavailable")
        # Catalog/default reads stay off the serial mutation/query command lane
        # so opening New Chat can never delay an immediate NewSession or Interrupt.
        # Reliable command ids coalesce retries until the original task ACKs.
        self._models_command_tasks: dict[
            tuple[str, str], asyncio.Task
        ] = {}
        self._status_command_tasks: dict[
            tuple[str, str], asyncio.Task
        ] = {}
        self._capabilities_command_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._interactive_control_tasks: dict[
            tuple[str, str], asyncio.Task
        ] = {}
        self._session_list_command_tasks: set[asyncio.Task] = set()
        self._codex = CodexBridge(self)
        self._codex_start_task = None

    def _clean_notification_title(cls, value: object) -> Optional[str]:
        if not isinstance(value, str):
            return None
        # Newlines become word boundaries; other control/format characters are
        # removed. A small input cap prevents a first prompt from turning title
        # formatting into another large-history path.
        cleaned = "".join(
            " " if char.isspace()
            else "" if unicodedata.category(char).startswith("C")
            else char
            for char in value[:2048]
        )
        normalized = " ".join(cleaned.split())
        return normalized[:cls.NOTIFICATION_TITLE_LENGTH].strip() or None

    def _remember_notification_title(self, sid: Optional[str], value: object) -> None:
        if not sid:
            return
        title = self._clean_notification_title(value)
        if not title:
            return
        self._notification_titles[sid] = title
        self._notification_titles.move_to_end(sid)
        while len(self._notification_titles) > self.NOTIFICATION_TITLE_CAP:
            self._notification_titles.popitem(last=False)

    def _notification_context(
        self,
        ctx: SessionContext,
    ) -> TurnNotificationContext:
        route_sid = (
            ctx.parent_sid if ctx.btw and ctx.parent_sid
            else (ctx.key or ctx.session_id)
        )
        display_name = self._notification_titles.get(route_sid or "")
        return TurnNotificationContext(
            engine=(ctx.engine if ctx.engine in {"claude", "codex", "dsh"}
                    else "claude"),
            space="work" if ctx.space == "work" else "code",
            display_name=display_name,
            parent_session_id=ctx.parent_sid if ctx.btw else None,
        )

    def _engine_catalog(
        self,
        *,
        to: str | None = None,
        route_id: str | None = None,
    ) -> EngineCatalog:
        return EngineCatalog(
            engines=[
                EngineInfo(
                    id="codex", display_name="Codex",
                    available=self._codex.available, spaces=["code"],
                    reason=self._codex.reason,
                ),
                EngineInfo(
                    id="dsh", display_name="DeepSeek Harness",
                    available=self._dsh_available, spaces=["code"],
                    reason=(None if self._dsh_available
                            else self._dsh_unavailable_reason),
                ),
            ],
            to=to,
            route_id=route_id,
        )

    @staticmethod
    def _dsh_preset_rows(
        preflight: DshPreflight | None,
    ) -> list[DshPresetInfo]:
        if preflight is None:
            return []
        result: list[DshPresetInfo] = []
        for row in preflight.presets[:128]:
            preset_id = row.get("id")
            trust = row.get("trust")
            if (
                not isinstance(preset_id, str)
                or not preset_id
                or len(preset_id) > 256
                or trust not in {"system", "user"}
            ):
                continue
            name = row.get("name")
            description = row.get("description")
            broken = row.get("broken")
            result.append(DshPresetInfo(
                id=preset_id,
                name=(name if isinstance(name, str) and name else preset_id)[:256],
                description=(description[:4096]
                             if isinstance(description, str) else None),
                trust=trust,
                is_default=row.get("isDefault") is True,
                broken=(broken[:4096] if isinstance(broken, str) else None),
            ))
        return result

    def _dsh_ctx_for_native(self, native_sid: str) -> SessionContext | None:
        try:
            wire_sid = dsh_wire_session_id(native_sid)
        except ValueError:
            return None
        ctx = self.sessions.get(wire_sid)
        return ctx if ctx is not None and ctx.engine == "dsh" else None

    def _dsh_client_ready(self) -> tuple[DshClient, DshHistory]:
        client = self._dsh_client
        history = self._dsh_history
        if (
            client is None
            or history is None
            or not self._dsh_available
        ):
            raise DshUnavailable(
                self._dsh_unavailable_reason or "DSH 本机服务未连接"
            )
        return client, history

    @staticmethod
    def _dsh_projection_title(row: dict) -> str | None:
        projections = row.get("projections")
        values = (
            projections.get("values")
            if isinstance(projections, dict) else None
        )
        title = values.get("title") if isinstance(values, dict) else None
        if not isinstance(title, str):
            return None
        normalized = " ".join(title.split())
        return normalized[:500] or None

    async def _read_dsh_catalog(
        self,
    ) -> tuple[list[dict], set[str]]:
        client, _history = self._dsh_client_ready()
        # DSH 0.2.0's roster is unfilterable and heavy, and one user action
        # triggers several reads (sidebar refresh, then attach). Collapse the
        # burst onto one DSH round trip; callers get their own row copies so a
        # mutation cannot leak into the cache.
        async with self._dsh_catalog_lock:
            cached = self._dsh_catalog_cache
            if (
                cached is not None
                and time.monotonic() - cached[0] < self.DSH_CATALOG_TTL_SECONDS
            ):
                return [dict(row) for row in cached[1]], set(cached[2])
            rows, archived = await self._fetch_dsh_catalog(client)
            self._dsh_catalog_cache = (time.monotonic(), rows, archived)
            return [dict(row) for row in rows], set(archived)

    async def _fetch_dsh_catalog(
        self, client: DshClient,
    ) -> tuple[list[dict], set[str]]:
        # DSH 0.1.2 removed workspace.list; the archived-session roster is now
        # maintained from workspace/archiveSession receipts (see
        # _dsh_archived_session_ids) and workspace/follow, not re-queried per
        # catalog read.  The session roster is the sole blocking read here.
        sessions_raw = await client.call("session.list")
        rows = (
            sessions_raw.get("items")
            if isinstance(sessions_raw, dict) else None
        )
        if (
            not isinstance(rows, list)
            or not all(isinstance(row, dict) for row in rows)
        ):
            raise DshProtocolError("DSH session catalog has an invalid shape")
        # DSH 0.2.0 will only page/follow a subagent Session through its durable
        # parent address, and the roster is the sole place that identity is
        # published.  Remember it here so history and follow can address every
        # session the catalog knows, not just the non-subagent rows the sidebar
        # exposes.
        remember = getattr(client, "remember_session_row", None)
        if callable(remember):
            for row in rows:
                remember(row)
        return [dict(row) for row in rows], set(self._dsh_archived_session_ids)

    async def _list_dsh_sessions(self, cmd) -> SessionList | Error:
        client_id = getattr(cmd, "client_id", None)
        if getattr(cmd, "space", "code") != "code":
            error = Error(
                code=ERR_AUTH,
                message="DeepSeek Harness 首版仅支持 Code 空间",
                request_id=getattr(cmd, "cmd_id", None),
                to=client_id,
            )
            await self.transport.send(error)
            return error
        try:
            rows, archived_ids = await self._read_dsh_catalog()
            pinned_ids = (
                self._dsh_session_pins.ids("dsh")
                if self._dsh_session_pins is not None else frozenset()
            )
            sessions: list[SessionInfo] = []
            # DSH v1 has no list cursor yet. Keep one sidebar response bounded
            # like the native Claude/Codex catalogs instead of forwarding an
            # arbitrarily large local catalog through the relay.
            for row in rows:
                if len(sessions) >= 400:
                    break
                native_sid = row.get("sessionId")
                if not isinstance(native_sid, str):
                    continue
                try:
                    wire_sid = dsh_wire_session_id(native_sid)
                except ValueError:
                    continue
                # Subagents are rendered inside their owning DSH turn; exposing
                # them as top-level cc-remote sessions would grant a second,
                # misleading navigation/control surface.
                if row.get("origin") == "subagent":
                    continue
                updated_at = row.get("updatedAt")
                running = row.get("running")
                blank = row.get("blank")
                if (
                    isinstance(updated_at, bool)
                    or not isinstance(updated_at, (int, float))
                    or not math.isfinite(float(updated_at))
                    or updated_at < 0
                    or not isinstance(running, bool)
                    or not isinstance(blank, bool)
                ):
                    continue
                resident = self.sessions.get(wire_sid)
                # DSH persists a freshly-created session before it has any
                # durable conversation content.  A cold blank row is not yet
                # useful history and may be an abandoned Web/TUI draft, so do
                # not turn it into a permanent sidebar entry.  Keep a resident
                # blank session visible: cc-remote owns that optimistic route
                # while its first prompt is being admitted.
                if blank and (
                    resident is None or resident.engine != "dsh"
                ):
                    continue
                cwd = row.get("cwd")
                cwd = cwd[:4096] if isinstance(cwd, str) and cwd else None
                title = self._dsh_projection_title(row)
                if title is None and cwd:
                    title = os.path.basename(cwd.rstrip(os.sep))[:500] or None
                if title is None:
                    title = "DeepSeek Harness"
                preset = row.get("agentPreset")
                preset = (
                    preset[:256]
                    if isinstance(preset, str) and preset else None
                )
                parent = row.get("parentSessionId")
                try:
                    parent_wire = (
                        dsh_wire_session_id(parent)
                        if isinstance(parent, str) else None
                    )
                except ValueError:
                    parent_wire = None
                state = (
                    resident.state
                    if resident is not None and resident.engine == "dsh"
                    else "running" if running else "idle"
                )
                sessions.append(SessionInfo(
                    session_id=wire_sid,
                    native_session_id=native_sid,
                    summary=title,
                    last_modified=str(updated_at),
                    cwd=cwd,
                    git_branch=(
                        row.get("gitBranch")
                        if isinstance(row.get("gitBranch"), str)
                        and row.get("gitBranch") else None
                    ),
                    tag="archived" if native_sid in archived_ids else None,
                    pinned=wire_sid in pinned_ids,
                    state=state,
                    engine="dsh",
                    space="code",
                    forked_from_id=parent_wire,
                    dsh_agent_preset=preset,
                    **self._session_presentation_fields("dsh", wire_sid),
                ))
                self._remember_notification_title(wire_sid, title)
            event = SessionList(
                engine="dsh",
                space="code",
                request_id=getattr(cmd, "cmd_id", None),
                sessions=sessions,
                dsh_presets=self._dsh_preset_rows(self._dsh_preflight),
                default_dsh_preset_id=(
                    self._dsh_preflight.default_preset_id
                    if self._dsh_preflight is not None else None
                ),
                to=client_id,
            )
            await self.transport.send(event)
            return event
        except Exception as exc:
            log.warning(
                "DSH session list failed",
                error_type=type(exc).__name__,
            )
            error = Error(
                code=ERR_INTERNAL,
                message="DeepSeek Harness 会话列表暂不可用，请稍后重试。",
                request_id=getattr(cmd, "cmd_id", None),
                to=client_id,
            )
            await self.transport.send(error)
            return error

    def _track_dsh_task(self, coroutine, *, name: str) -> bool:
        if len(self._dsh_interaction_tasks) >= self.DSH_INTERACTION_TASK_CAP:
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
            log.warning(
                "DSH background task capacity reached",
                task=name,
                cap=self.DSH_INTERACTION_TASK_CAP,
            )
            return False
        task = asyncio.create_task(coroutine, name=name)
        self._dsh_interaction_tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self._dsh_interaction_tasks.discard(done)
            if done.cancelled():
                return
            try:
                done.result()
            except Exception as exc:
                log.warning(
                    "DSH background event handling failed",
                    task=name,
                    error_type=type(exc).__name__,
                )

        task.add_done_callback(finished)
        return True

    async def _reopen_dsh_follows(self) -> None:
        """Ensure every resident DSH session has a live follow stream.

        DSH 0.1.2 streams turn events only through per-session follow streams
        on the multiplexed carrier.  ``_mux_connect`` starts with an empty
        stream map, so after any reconnect the resident sessions must be
        re-followed or their live content is silently lost.
        """
        client = self._dsh_client
        if client is None:
            return
        for ctx in tuple(self.sessions.values()):
            if ctx.engine != "dsh" or not ctx.session_id:
                continue
            try:
                await client.follow_session(ctx.session_id)
            except Exception as exc:
                log.warning(
                    "DSH follow reopen failed",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )

    async def _on_dsh_availability(
        self,
        available: bool,
        preflight: DshPreflight | None,
        reason: str | None,
    ) -> None:
        changed = (
            available != self._dsh_available
            or reason != self._dsh_unavailable_reason
        )
        self._dsh_available = available
        self._dsh_preflight = preflight
        self._dsh_unavailable_reason = reason
        if available:
            # A reconnected remote.mux carrier drops every logical stream,
            # including each resident session's follow feed.  Without reopening
            # them a live turn still flips the session to running through the
            # $events status feed while its content never streams — the browser
            # shows "thinking" forever with no output.  follow_session is
            # idempotent for already-open sessions, so this is safe to re-run on
            # every availability announcement.
            await self._reopen_dsh_follows()
        if not changed:
            return
        try:
            await self.transport.send(self._engine_catalog())
            await self._invalidate_session_list("dsh", "code")
        except Exception as exc:
            # Relay availability must not tear down the healthy loopback streams.
            log.warning(
                "DSH availability update could not be published",
                error_type=type(exc).__name__,
            )
        if available:
            for ctx in tuple(self.sessions.values()):
                if ctx.engine == "dsh" and ctx.session_id:
                    reconciliation = (
                        self._reconcile_dsh_prompt(
                            ctx, ctx.dsh_uncertain_msg_id,
                        )
                        if ctx.dsh_uncertain_msg_id
                        else self._reconcile_dsh_steer(
                            ctx,
                            ctx.dsh_uncertain_steer_id,
                            notify_rejected=True,
                        )
                        if ctx.dsh_uncertain_steer_id
                        else self._reconcile_dsh_resident(ctx)
                    )
                    self._track_dsh_task(
                        reconciliation,
                        name=f"cc-remote-dsh-history-{ctx.session_id}",
                    )

    async def _reconcile_dsh_resident(
        self,
        ctx: SessionContext,
        *,
        expected_msg_id: str | None = None,
    ) -> bool | None:
        """Reconcile one resident from DSH's source log and current catalog.

        ``True`` proves a particular browser message was accepted, ``False``
        proves it was not accepted while the native session is idle, and
        ``None`` preserves uncertainty.  The same read also repairs a terminal
        event missed during a stream reconnect.
        """
        sid = self._ctx_wire_sid(ctx)
        native_sid = ctx.session_id
        if ctx.engine != "dsh" or not sid or not native_sid:
            return None
        if self.sessions.get(ctx.key or sid) is not ctx:
            return None
        expected_was_pending = bool(
            expected_msg_id is not None
            and expected_msg_id in {
                ctx.dsh_uncertain_msg_id,
                ctx.dsh_uncertain_steer_id,
            }
        )
        client, history_store = self._dsh_client_ready()
        # Reserve ordering before starting I/O. A concurrent GetHistory begun
        # later must always own a larger sequence even if its DSH read finishes
        # first; otherwise this reconciliation could overwrite newer content.
        build_seq = self._history_build_sequences.get(sid, 0) + 1
        self._history_build_sequences[sid] = build_seq
        page = await history_store.page(
            native_sid,
            wire_session_id=sid,
            before=None,
            limit=8,
        )
        accepted = (
            any(turn.clientMsgId == expected_msg_id for turn in page.turns)
            or any(
                    (
                        event.get("type") == "user_msg"
                        and event.get("client_msg_id") == expected_msg_id
                    )
                    or (
                        event.get("type") == "turn_steered"
                        and expected_msg_id in {
                            event.get("msg_id"), event.get("client_msg_id"),
                        }
                    )
                    for event in page.events
            )
            if expected_msg_id is not None else None
        )
        async with ctx.dsh_event_lock:
            if page.last_seq >= ctx.dsh_last_source_seq:
                ctx.dsh_last_source_seq = page.last_seq
                ctx.dsh_translator = page.translator
            translator = ctx.dsh_translator
            active = bool(
                isinstance(translator, DshStreamTranslator)
                and translator.active_turn is not None
            )
        # DSH history is cut at a bounded message window. A long active turn
        # can therefore yield a valid tail page whose turn/start is older than
        # the cut; translator.active_turn alone is not proof that the native
        # session is idle. Consult the catalog whenever the source tail cannot
        # prove activity (and when resolving a missing receipt).
        catalog_running: bool | None = None
        if not active or (expected_msg_id is not None and not accepted):
            raw = await client.call("session.list")
            rows = raw.get("items") if isinstance(raw, dict) else None
            if not isinstance(rows, list):
                raise DshProtocolError("session.list omitted items")
            row = next((
                item for item in rows
                if isinstance(item, dict)
                and item.get("sessionId") == native_sid
            ), None)
            if row is None:
                raise DshProtocolError("DSH resident disappeared from catalog")
            remember = getattr(client, "remember_session_row", None)
            if callable(remember):
                remember(row)
            running = row.get("running")
            if not isinstance(running, bool):
                raise DshProtocolError("DSH catalog omitted running state")
            catalog_running = running
        # The mux may publish the exact user identity or even a whole new turn
        # while ``session.list`` is in flight. Re-enter its ordering lane before
        # applying the older catalog observation: otherwise a live acceptance
        # can be reported as rejected and a fresh running state overwritten by
        # idle. A marker can only disappear here through source-bound live or a
        # concurrent source-bound reconciliation.
        async with ctx.dsh_event_lock:
            translator = ctx.dsh_translator
            active = bool(
                isinstance(translator, DshStreamTranslator)
                and translator.active_turn is not None
            )
            live_resolved = bool(
                expected_was_pending
                and expected_msg_id not in {
                    ctx.dsh_uncertain_msg_id,
                    ctx.dsh_uncertain_steer_id,
                }
            )
            if live_resolved:
                accepted = True
            source_running = bool(
                active
                or catalog_running is True
                # A direct user/message can precede turn/start on the mux. Keep
                # the query's admitted running state through that short gap; a
                # live terminal would already have changed it back to idle.
                or (live_resolved and ctx.state != "idle")
            )
            # Publish the canonical page before changing state so a browser never
            # sees idle while its exact terminal/content repair is still in flight.
            projected = History(
                session_id=sid,
                revision=self._history_revision(sid),
                generation=self.instance_id,
                build_seq=build_seq,
                live_seq=ctx.seq,
                events=[],
                turns=list(page.turns),
                detail="summary",
                has_more=page.has_more,
                oldest_id=page.oldest_id,
                newest_id=page.newest_id,
                control=self._session_control(ctx),
                external=False,
                # A bounded tail can omit turn/start, while catalog/status can
                # announce running before mux publishes it. Preserve either source
                # of positive activity across those independent streams.
                in_progress=source_running,
            )
            projected.sid = sid
            await self.transport.send(projected)

            if not source_running:
                ctx.active_msg_id = None
                ctx.interrupt_deadline = None
                ctx.interrupt_event.clear()
                if expected_msg_id == ctx.dsh_uncertain_msg_id:
                    ctx.dsh_uncertain_msg_id = None
                if ctx.state != "idle":
                    await self._set_state(ctx, "idle")
                await self.transport.send(SessionActivity(
                    engine="dsh", session_id=sid, state="idle",
                ))
            elif ctx.state != "running":
                ctx.interrupt_deadline = None
                ctx.interrupt_event.clear()
                await self._set_state(ctx, "running")
        if expected_msg_id is None:
            return None
        if accepted:
            if ctx.dsh_uncertain_msg_id == expected_msg_id:
                ctx.dsh_uncertain_msg_id = None
            return True
        if catalog_running is False and not active:
            return False
        return None

    async def _reconcile_dsh_terminal(self, ctx: SessionContext) -> None:
        # Give the independent mux reader one scheduling turn to deliver the
        # exact terminal before falling back to a source-log read.
        await asyncio.sleep(0)
        if ctx.state == "idle":
            return
        try:
            await self._reconcile_dsh_resident(ctx)
        except Exception as exc:
            log.debug(
                "DSH terminal reconciliation deferred",
                session_id=self._ctx_wire_sid(ctx),
                error_type=type(exc).__name__,
            )

    async def _reconcile_dsh_terminal_after_interrupt(
        self, ctx: SessionContext,
    ) -> None:
        """Settle an interrupted DSH turn from the source log.

        DSH 0.1.2 may cancel a turn without emitting a terminal turn/end on
        the follow stream.  Retry the reconcile a few times over a short
        window: a genuinely stopping agent can take a moment to clear its
        running flag, while an idle session must return to idle promptly so
        the next message is accepted.
        """
        for index, delay in enumerate((0.0, 0.5, 1.5, 3.0)):
            if delay:
                await asyncio.sleep(delay)
            if ctx.state == "idle" or self.sessions.get(ctx.key) is not ctx:
                return
            try:
                await self._reconcile_dsh_resident(ctx)
            except Exception as exc:
                log.debug(
                    "DSH post-interrupt reconciliation deferred",
                    session_id=self._ctx_wire_sid(ctx),
                    attempt=index,
                    error_type=type(exc).__name__,
                )
                continue
            if ctx.state == "idle":
                return
        # Final fallback: if the source log and catalog still report activity
        # after the retry window, the session is genuinely still running (or the
        # carrier is unreachable).  Leave it running rather than force idle —
        # a wrong idle would accept a query into an actually-busy turn.
        log.info(
            "DSH interrupt settle still running",
            session_id=self._ctx_wire_sid(ctx),
        )

    async def _reconcile_dsh_terminal_after_agent_error(
        self, ctx: SessionContext, message: str,
    ) -> None:
        """Reconcile after an agent/error event without racing its terminal.

        The $events stream reports an agent error before the follow stream
        delivers the authoritative turn/end.  Wait through a few settle rounds:
        a recoverable error (request retry waterfall, transient tool failure)
        leaves the agent running or completes normally, in which case no
        browser Error should be shown.  Only when the source log confirms the
        session went idle without a normal terminal do we surface the failure.
        """
        for delay in (0.0, 0.5, 1.5, 3.0):
            if delay:
                await asyncio.sleep(delay)
            if self.sessions.get(ctx.key) is not ctx:
                return
            if ctx.state == "idle":
                # The follow stream settled the terminal (normal or error) and
                # the reconcile already published the authoritative history.
                return
            try:
                await self._reconcile_dsh_resident(ctx)
            except Exception as exc:
                log.debug(
                    "DSH agent-error reconciliation deferred",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )
                continue
            if ctx.state == "idle":
                return
        # The session is still marked running after the settle window and the
        # source log could not prove a terminal.  Surface the reported error
        # only now, when it is genuinely stuck rather than merely in flight.
        if self.sessions.get(ctx.key) is ctx and ctx.state != "idle":
            log.warning(
                "DSH agent error left turn unsettled",
                session_id=self._ctx_wire_sid(ctx),
                message=message[:512],
            )

    async def _reconcile_dsh_prompt(
        self, ctx: SessionContext, msg_id: str,
    ) -> None:
        """Resolve a prompt whose HTTP receipt was lost without duplicating it."""
        for delay in self.DSH_RECEIPT_RECONCILE_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            if ctx.dsh_uncertain_msg_id != msg_id:
                return
            try:
                accepted = await self._reconcile_dsh_resident(
                    ctx, expected_msg_id=msg_id,
                )
            except Exception as exc:
                log.debug(
                    "DSH prompt reconciliation deferred",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )
                continue
            if accepted is True:
                return
            if accepted is False:
                ctx.dsh_uncertain_msg_id = None
                ctx.active_msg_id = None
                if ctx.state != "idle":
                    await self._set_state(ctx, "idle")
                await self._emit(ctx, Error(
                    code=ERR_INTERNAL,
                    message="DeepSeek Harness 未接收本次消息，请重试。",
                    msg_id=msg_id,
                ))
                return
        # The host may be temporarily unavailable.  Preserve the uncertain
        # marker and running state; availability recovery will perform the same
        # source-bound reconciliation instead of guessing or inviting a resend.
        log.info(
            "DSH prompt outcome remains uncertain",
            session_id=self._ctx_wire_sid(ctx),
            msg_id=msg_id,
        )

    async def _reconcile_dsh_steer(
        self,
        ctx: SessionContext,
        msg_id: str,
        *,
        notify_rejected: bool,
    ) -> bool | None:
        """Resolve one lost DSH steer receipt from its exact source identity.

        Unlike a first prompt, a steer is already inside a running turn, so an
        unknown outcome must not change the resident state.  ``None`` remains
        fail-closed: the caller tells the user not to repeat the mutation while
        the source stream/history continues the reconciliation.
        """
        for delay in self.DSH_RECEIPT_RECONCILE_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            if ctx.dsh_uncertain_steer_id != msg_id:
                return True
            try:
                accepted = await self._reconcile_dsh_resident(
                    ctx, expected_msg_id=msg_id,
                )
            except Exception as exc:
                log.debug(
                    "DSH steer reconciliation deferred",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )
                continue
            if accepted is True:
                ctx.dsh_uncertain_steer_id = None
                return True
            if accepted is False:
                ctx.dsh_uncertain_steer_id = None
                if notify_rejected:
                    await self._emit(ctx, Error(
                        code=ERR_NOT_STEERABLE,
                        message="DeepSeek Harness 未接收本次引导。",
                        msg_id=msg_id,
                    ))
                return False
        log.info(
            "DSH steer outcome remains uncertain",
            session_id=self._ctx_wire_sid(ctx),
            msg_id=msg_id,
        )
        return None

    def _remember_pending_waterfall(self, wire_sid: str, frame: dict) -> None:
        """Hold an interactive request until its session becomes resident."""
        entry = self._pending_waterfalls.setdefault(wire_sid, [])
        request_id = frame.get("rpcId")
        for existing in entry:
            if existing.get("rpcId") == request_id:
                return
        entry.append({**frame, "_held_at": time.time()})
        # A stuck DSH agent can only raise a bounded number of live requests.
        if len(entry) > self.PENDING_WATERFALL_CAP:
            del entry[0]
        log.info(
            "DSH interactive request held until resume",
            session_id=wire_sid,
            type=frame.get("payload", {}).get("type"),
            request_id=request_id,
            held=len(entry),
        )

    async def _flush_pending_waterfalls(self, ctx: SessionContext) -> None:
        """Deliver interactive requests that can still be awaiting an answer.

        The cache covers one race: a waterfall that DSH raised just before its
        session became resident. It is NOT a durable queue — DSH never resends a
        waterfall, but it also never tells us when one was answered elsewhere,
        so replaying an old entry resurrects a prompt the user already answered
        (and creates a fresh blocked interaction that forces a choice). Two
        guards keep the replay honest: an entry older than
        ``HELD_WATERFALL_TTL`` is dropped, and so is any entry for a session that
        is already idle — an agent waiting on an approval or a question is by
        definition inside a running turn.
        """
        wire_sid = self._ctx_wire_sid(ctx)
        frames = self._pending_waterfalls.pop(wire_sid, None) if wire_sid else None
        if not frames:
            return
        now = time.time()
        live: list[dict] = []
        for frame in frames:
            held_at = frame.get("_held_at")
            age = now - held_at if isinstance(held_at, (int, float)) else None
            if age is not None and age > self.HELD_WATERFALL_TTL:
                log.info(
                    "DSH held request discarded as stale",
                    session_id=wire_sid,
                    request_id=frame.get("rpcId"),
                    age_seconds=round(age),
                )
                continue
            if ctx.state == "idle":
                log.info(
                    "DSH held request discarded: session is idle",
                    session_id=wire_sid,
                    request_id=frame.get("rpcId"),
                )
                continue
            live.append(frame)
        if not live:
            return
        log.info(
            "DSH interactive requests replayed after resume",
            session_id=wire_sid,
            count=len(live),
        )
        for frame in live:
            try:
                await self._on_dsh_mux({k: v for k, v in frame.items()
                                        if k != "_held_at"})
            except Exception as exc:
                log.warning(
                    "DSH held request replay failed",
                    session_id=wire_sid,
                    error_type=type(exc).__name__,
                )

    async def _refresh_dsh_permission_catalog(self) -> None:
        """Re-read the process-level permission catalog and re-announce it.

        DSH 0.2.0 emits ``permission-presets/catalog-changed`` without a payload
        by design: a subscriber re-reads the whole catalog.  Every resident
        renders its own switcher, so all of them are re-announced.
        """
        client = self._dsh_client
        refresh = getattr(client, "refresh_permission_catalog", None)
        if client is None or not callable(refresh):
            return
        try:
            await refresh()
        except (DshError, ValueError) as exc:
            log.warning(
                "DSH permission catalog refresh failed",
                error_type=type(exc).__name__,
            )
            return
        options = getattr(client, "permission_options", ())
        for ctx in list(self.sessions.values()):
            if ctx.engine != "dsh" or not isinstance(ctx.sdk, DshSessionHandle):
                continue
            ctx.sdk.permission_options = options
            permission = _session_permission_event(ctx)
            if permission is not None:
                ctx.announced_perm = permission.mode
                await self._emit(ctx, permission)

    async def _on_dsh_host(self, frame: dict) -> None:
        payload = frame["payload"]
        frame_type = payload.get("type")
        native_sid = payload.get("sessionId")
        if frame_type == "host/archived-sessions-changed":
            archived = payload.get("archivedSessionIds")
            if not isinstance(archived, list) or not all(
                isinstance(item, str) for item in archived
            ):
                raise DshProtocolError("DSH archived-session state is invalid")
            await self._invalidate_session_list("dsh", "code")
            return
        if frame_type == "permission-presets/catalog-changed":
            # Payload-free by design: re-read the catalog, then re-announce the
            # switcher to every resident that renders one.
            await self._refresh_dsh_permission_catalog()
            return
        if not isinstance(native_sid, str):
            # Workspace and remote Cordis events do not address a conversation.
            return
        wire_sid = dsh_wire_session_id(native_sid)
        ctx = self.sessions.get(wire_sid)
        if frame_type == "host/session-status":
            running = payload.get("running")
            if not isinstance(running, bool):
                raise DshProtocolError("DSH status frame omitted running")
            if running:
                if ctx is not None and ctx.engine == "dsh":
                    if ctx.state != "running":
                        await self._set_state(ctx, "running")
                await self.transport.send(SessionActivity(
                    engine="dsh",
                    session_id=wire_sid,
                    state="running",
                ))
            elif ctx is not None and ctx.engine == "dsh":
                # Host status and session events use independent WebSockets. A
                # false status may overtake the exact terminal turn/end, so do
                # not close the browser turn from this advisory signal.
                self._track_dsh_task(
                    self._reconcile_dsh_terminal(ctx),
                    name=f"cc-remote-dsh-terminal-{native_sid}",
                )
            else:
                # Cold sidebar rows have no resident turn projection to race.
                await self.transport.send(SessionActivity(
                    engine="dsh",
                    session_id=wire_sid,
                    state="idle",
                ))
            return
        if frame_type == "host/agent-error":
            message = payload.get("message")
            if ctx is None or not isinstance(message, str):
                return
            # DSH 0.1.2 reports an agent error on the $events stream BEFORE the
            # follow stream delivers the authoritative turn/end.  Failing the
            # browser turn here races that terminal: an LLM request error can
            # surface as a recoverable waterfall (retry) that the agent then
            # completes, so immediately killing the turn with an Error produces
            # the false "该轮未正常结束" the user reports.  Let the source
            # log/catalog settle the exact terminal instead.
            self._track_dsh_task(
                self._reconcile_dsh_terminal_after_agent_error(ctx, message),
                name=(
                    "cc-remote-dsh-agent-error-"
                    f"{ctx.session_id or ctx.key}"
                ),
            )
            return
        if frame_type == "host/session-removed" and ctx is not None:
            self.sessions.pop(wire_sid, None)
            if self.focused_sid == wire_sid:
                self.focused_sid = None
        if frame_type in {"host/session-added", "host/session-removed"}:
            await self._invalidate_session_list("dsh", "code")

    async def _announce_dsh_selection(self, ctx: SessionContext) -> None:
        """Publish the session's live DSH model and reasoning strength.

        Both come from the ``modelSelection`` projection, which in DSH 0.2.0 is
        the only per-session source: ``session/modelCatalog`` now reports just a
        provider/model pair with no ``reasoningEffort``.
        """
        model = _session_model(ctx)
        if model is not None:
            ctx.announced_model = model
            await self._emit(ctx, Model(model=model))
        effort = _session_effort(ctx)
        if effort is not None:
            ctx.announced_effort = effort
            await self._emit(ctx, Effort(effort=effort))

    async def _on_dsh_mux(self, frame: dict) -> None:
        payload = frame["payload"]
        frame_type = payload.get("type")
        native_sid = payload.get("sessionId")
        if frame_type == "stream/error":
            return
        if not isinstance(native_sid, str):
            raise DshProtocolError("DSH mux frame omitted sessionId")
        wire_sid = dsh_wire_session_id(native_sid)
        ctx = self.sessions.get(wire_sid)
        # Interactive waterfalls (approvals/questions) block DSH's agent until
        # answered.  One can arrive before its session is resident (fresh page
        # load, or a reconnect that interleaves the waterfall retry ahead of
        # the attach) and DSH does not resend it, so dropping it would strand
        # the agent with no visible prompt.  Hold it until the session resumes.
        if frame_type in {"approval/requested", "question/requested"}:
            if ctx is None or ctx.engine != "dsh":
                self._remember_pending_waterfall(wire_sid, frame)
                return
        if frame_type == "session/event":
            event = payload.get("event")
            if not isinstance(event, dict):
                raise DshProtocolError("DSH mux event omitted event")
            # Settle prompts DSH closed without telling us, BEFORE the resident
            # checks below: a question that arrived for a not-yet-resident
            # session is exactly the case that used to be replayed forever.
            settled_type = str(event.get("type"))
            settled_data = event.get("data")
            if not isinstance(settled_data, dict):
                settled_data = {}
            if settled_type == "approval/asked":
                asked_id = settled_data.get("id")
                asked_call = settled_data.get("callId")
                if isinstance(asked_id, str) and isinstance(asked_call, str):
                    self._dsh_approval_calls[(wire_sid, asked_id)] = asked_call
            elif settled_type == "approval/decided":
                decided_id = settled_data.get("id")
                if isinstance(decided_id, str):
                    # approval/decided names the audit id; the call id is what
                    # our pending approval (and the tool result) is keyed on.
                    settled_call = self._dsh_approval_calls.pop(
                        (wire_sid, decided_id), None
                    )
                    if settled_call:
                        self._resolve_dsh_interactions(
                            ctx, wire_sid, approval_keys=(settled_call,),
                        )
            elif settled_type == "tool/call":
                call_id = settled_data.get("callId")
                if isinstance(call_id, str) and call_id:
                    self._dsh_last_tool_call[wire_sid] = call_id
            elif settled_type == "tool/result":
                message = settled_data.get("message")
                source = (
                    message.get("source") if isinstance(message, dict) else None
                )
                call_id = (
                    source.get("callId") if isinstance(source, dict) else None
                )
                finished = self._dsh_requests_for_call(wire_sid, call_id)
                if finished:
                    self._resolve_dsh_interactions(
                        ctx, wire_sid, request_ids=finished,
                    )
                if isinstance(call_id, str) and call_id:
                    # The decided tool call also settles its approval, even if
                    # the audit pair never reached this connection.
                    self._resolve_dsh_interactions(
                        ctx, wire_sid, approval_keys=(call_id,),
                    )
            elif settled_type == "turn/end":
                # A turn cannot end with a question still waiting for an answer.
                pending = self._dsh_requests_for_session(wire_sid)
                if pending:
                    self._resolve_dsh_interactions(
                        ctx, wire_sid, request_ids=pending,
                    )
            source_seq = event.get("seq")
            if not isinstance(source_seq, int) or isinstance(source_seq, bool):
                raise DshProtocolError("DSH mux event omitted seq")
            if ctx is None or ctx.engine != "dsh":
                if str(event.get("type")) in {"turn/start", "turn/end", "user/message"}:
                    log.warning(
                        "DSH event dropped: no resident context",
                        session_id=wire_sid,
                        type=str(event.get("type")),
                    )
                return
            async with ctx.dsh_event_lock:
                if source_seq <= ctx.dsh_last_source_seq:
                    if str(event.get("type")) in {"turn/start", "turn/end", "user/message"}:
                        log.warning(
                            "DSH event dropped: seq not newer",
                            session_id=wire_sid,
                            type=str(event.get("type")),
                            seq=source_seq,
                            watermark=ctx.dsh_last_source_seq,
                        )
                    return
                ctx.dsh_last_source_seq = source_seq
                if event.get("type") == "turn/start" and ctx.state != "running":
                    await self._set_state(ctx, "running")
                translator = ctx.dsh_translator
                if not isinstance(translator, DshStreamTranslator):
                    translator = DshStreamTranslator()
                    ctx.dsh_translator = translator
                event_type = event.get("type")
                command_id = None
                command_alias = None
                if event_type in {"command/run", "command/done"}:
                    data = event.get("data")
                    command_id = (
                        data.get("commandId")
                        if isinstance(data, dict) else None
                    )
                    if isinstance(command_id, str):
                        command_alias = ctx.dsh_command_aliases.get(command_id)
                    if (
                        event_type == "command/run"
                        and isinstance(command_id, str)
                        and ctx.dsh_pending_command_msg_id is not None
                        and ctx.dsh_pending_command_prompt is not None
                        and isinstance(data, dict)
                    ):
                        name = data.get("name")
                        args = data.get("args")
                        native_prompt = (
                            f"/{name}{args or ''}"
                            if isinstance(name, str)
                            and (args is None or isinstance(args, str))
                            else None
                        )
                        if native_prompt == ctx.dsh_pending_command_prompt:
                            command_alias = ctx.dsh_pending_command_msg_id
                            self._remember_dsh_command_alias(
                                ctx, command_id, command_alias,
                            )
                try:
                    translated = translator.feed(
                        event,
                        view=payload.get("view"),
                        command_client_id=command_alias,
                    )
                except DshEventError as exc:
                    raise DshProtocolError(str(exc)) from exc
                suppress_command_lifecycle = bool(
                    event_type in {"command/run", "command/done"}
                    and command_alias is not None
                )
                for message in translated:
                    # commands/execute settles with the exact native commandId.
                    # Until then, emitting command/run would create a second
                    # user row beside the browser's optimistic message.  The
                    # settlement path publishes one aliased canonical page.
                    if suppress_command_lifecycle:
                        continue
                    if isinstance(message, (UserMsg, TurnSteered)):
                        history_store = self._dsh_history
                        remember_images = getattr(
                            history_store, "remember_event_images", None)
                        if callable(remember_images):
                            refs = remember_images(
                                wire_sid,
                                event,
                                message.msg_id,
                                message.client_msg_id,
                            )
                            if refs:
                                message.image_refs = refs
                    await self._emit(ctx, message)
                    if (
                        isinstance(message, UserMsg)
                        and message.client_msg_id == ctx.dsh_uncertain_msg_id
                    ):
                        ctx.dsh_uncertain_msg_id = None
                    if (
                        isinstance(message, TurnSteered)
                        and ctx.dsh_uncertain_steer_id in {
                            message.msg_id, message.client_msg_id,
                        }
                    ):
                        ctx.dsh_uncertain_steer_id = None
                    if (
                        isinstance(message, TurnEnd)
                        and event_type == "turn/end"
                    ):
                        ctx.active_msg_id = None
                        ctx.interrupt_deadline = None
                        ctx.interrupt_event.clear()
                        ctx.dsh_uncertain_msg_id = None
                        if ctx.state != "idle":
                            await self._set_state(ctx, "idle")
                        await self._invalidate_session_list("dsh", "code")
                if (
                    event_type == "command/done"
                    and isinstance(command_id, str)
                    and command_alias is not None
                    and ctx.dsh_pending_command_msg_id == command_alias
                ):
                    data = event.get("data")
                    if isinstance(data, dict):
                        await self._finish_dsh_command(
                            ctx,
                            msg_id=command_alias,
                            prompt=ctx.dsh_pending_command_prompt or "",
                            name=(
                                (ctx.dsh_pending_command_prompt or "")
                                .removeprefix("/").split(maxsplit=1)[0]
                                or "command"
                            ),
                            kind=(
                                data.get("kind")
                                if data.get("kind") in {"success", "error"}
                                else "error"
                            ),
                            text=(
                                data.get("text")
                                if isinstance(data.get("text"), str) else None
                            ),
                            command_id=command_id,
                        )
                if event_type == "session/title":
                    await self._invalidate_session_list("dsh", "code")
            return
        if frame_type == "session/assistant-stream":
            # DSH 0.2.0's live assistant presentation.  These frames are
            # process-local, so they deliberately bypass the durable seq
            # watermark below: advancing it here would make the next real
            # journal event look stale and get dropped.
            if ctx is None or ctx.engine != "dsh":
                return
            stream_turn = payload.get("turn")
            stream_step = payload.get("step")
            if not isinstance(stream_turn, int) or isinstance(stream_turn, bool):
                raise DshProtocolError("DSH assistant stream frame omitted turn")
            if not isinstance(stream_step, int) or isinstance(stream_step, bool):
                raise DshProtocolError("DSH assistant stream frame omitted step")
            translator = ctx.dsh_translator
            if not isinstance(translator, DshStreamTranslator):
                return
            async with ctx.dsh_event_lock:
                try:
                    streamed = translator.feed_stream_chunk(
                        stream_turn,
                        stream_step,
                        payload.get("chunk"),
                        payload.get("time"),
                    )
                except DshEventError as exc:
                    raise DshProtocolError(str(exc)) from exc
                for message in streamed:
                    await self._emit(ctx, message)
            return
        if frame_type == "session/subscribed":
            last_seq = payload.get("lastSeq")
            if not isinstance(last_seq, int) or isinstance(last_seq, bool):
                raise DshProtocolError("DSH subscription omitted lastSeq")
            if ctx is not None and ctx.engine == "dsh":
                ctx.dsh_last_source_seq = max(ctx.dsh_last_source_seq, last_seq)
            return
        if frame_type == "session/projection":
            key = payload.get("key")
            projection_seq = payload.get("seq")
            if (
                not isinstance(key, str)
                or not key
                or not isinstance(projection_seq, int)
                or isinstance(projection_seq, bool)
                or projection_seq < 0
                or projection_seq > 9_007_199_254_740_991
            ):
                raise DshProtocolError("DSH projection frame is invalid")
            if key == "title":
                await self._invalidate_session_list("dsh", "code")
                return
            if (
                ctx is not None
                and ctx.engine == "dsh"
                and key in {
                    "permissions", "modelSelection",
                    "contextPressure", "contextBreakdown",
                }
            ):
                if not isinstance(ctx.sdk, DshSessionHandle):
                    raise DshProtocolError(
                        "DSH projection target has no session handle"
                    )
                changed = ctx.sdk.apply_projection(
                    key, payload.get("value"), projection_seq,
                )
                if changed and key == "permissions":
                    permission = _session_permission_event(ctx)
                    if permission is not None:
                        ctx.announced_perm = permission.mode
                        await self._emit(ctx, permission)
                elif changed and key == "modelSelection":
                    # DSH 0.2.0 publishes the session's live route here; without
                    # this the composer keeps showing its loading placeholder
                    # for model and reasoning strength on every resume.
                    await self._announce_dsh_selection(ctx)
            return
        if frame_type in {"session/queue", "session/jobs"}:
            # DSH owns these transient plugin surfaces. cc-remote's first
            # release keeps its own durable send queue and renders job/tool
            # lifecycle from the ordinary session event stream.
            return
        if frame_type == "approval/requested":
            if ctx is not None and ctx.engine == "dsh":
                # DSH 0.1.2's approval request carries no approval id — only
                # the tool, its call id and a reason. Key on the call id, which
                # is also what approval/asked, approval/decided and the tool
                # result correlate on, and fall back to the request id.
                approval_key = payload.get("callId")
                if not isinstance(approval_key, str) or not approval_key:
                    approval_key = frame["rpcId"]
                self._dsh_pending_approvals[
                    (wire_sid, approval_key)
                ] = frame["rpcId"]
                tracked = self._track_dsh_task(
                    self._answer_dsh_approval(ctx, frame, approval_key),
                    name=f"cc-remote-dsh-approval-{native_sid}",
                )
                if not tracked:
                    self._dsh_pending_approvals.pop(
                        (wire_sid, approval_key), None
                    )
                    client = self._dsh_client
                    if client is not None:
                        await client.respond_error(
                            frame["rpcId"],
                            message="Remote interaction capacity was reached",
                        )
            return
        if frame_type == "question/requested":
            log.info(
                "DSH question requested",
                session_id=wire_sid,
                resident=ctx is not None,
                request_id=frame.get("rpcId"),
            )
            if ctx is not None and ctx.engine == "dsh":
                call_id = self._dsh_last_tool_call.get(wire_sid)
                if isinstance(call_id, str) and call_id:
                    self._dsh_request_calls[(wire_sid, frame["rpcId"])] = call_id
                tracked = self._track_dsh_task(
                    self._answer_dsh_questions(ctx, frame),
                    name=f"cc-remote-dsh-question-{native_sid}",
                )
                if not tracked:
                    client = self._dsh_client
                    if client is not None:
                        await client.respond_error(
                            frame["rpcId"],
                            message="Remote interaction capacity was reached",
                        )
            return
        if frame_type == "approval/resolved":
            approval_id = payload.get("approvalId")
            request_id = self._dsh_pending_approvals.pop(
                (wire_sid, approval_id), None
            ) if isinstance(approval_id, str) else None
            if request_id:
                self._cancel_dsh_request_asks(ctx, wire_sid, request_id)
            return
        if frame_type == "question/resolved":
            request_id = payload.get("questionRpcId")
            if isinstance(request_id, str):
                self._cancel_dsh_request_asks(ctx, wire_sid, request_id)

    def _resolve_dsh_interactions(
        self,
        ctx: SessionContext | None,
        wire_sid: str,
        *,
        approval_keys: tuple[str, ...] = (),
        request_ids: tuple[str, ...] = (),
    ) -> None:
        """Close prompts whose waterfall DSH settled somewhere else.

        DSH hands an approval/question to every connected client; when one of
        them answers, the gateway settles the waterfall **without telling the
        others**. The desktop therefore answered a dialog while the phone kept
        the card — and, because a request that arrived before its session was
        resident is cached for replay, the phone kept re-showing it on every
        later open. The session event stream is the only authoritative signal:
        ``approval/decided`` for approvals, and the completing tool call (or the
        ending turn) for a question.
        """
        for approval_key in approval_keys:
            request_id = self._dsh_pending_approvals.pop(
                (wire_sid, approval_key), None
            )
            if request_id:
                request_ids = (*request_ids, request_id)
        if not request_ids:
            return
        held = self._pending_waterfalls.get(wire_sid)
        for request_id in request_ids:
            if ctx is not None:
                self._cancel_dsh_request_asks(ctx, wire_sid, request_id)
            self._dsh_request_calls.pop((wire_sid, request_id), None)
            if held:
                held[:] = [
                    frame for frame in held
                    if frame.get("rpcId") != request_id
                ]
            log.info(
                "DSH interaction settled elsewhere",
                session_id=wire_sid,
                request_id=request_id,
            )
        if held is not None and not held:
            self._pending_waterfalls.pop(wire_sid, None)

    def _dsh_requests_for_call(
        self, wire_sid: str, call_id: str | None,
    ) -> tuple[str, ...]:
        """Pending question requests raised by one tool call."""
        if not isinstance(call_id, str) or not call_id:
            return ()
        return tuple(
            request_id
            for (session_id, request_id), owner in self._dsh_request_calls.items()
            if session_id == wire_sid and owner == call_id
        )

    def _dsh_requests_for_session(self, wire_sid: str) -> tuple[str, ...]:
        return tuple(
            request_id
            for (session_id, request_id) in self._dsh_pending_request_asks
            if session_id == wire_sid
        )

    def _cancel_dsh_request_asks(
        self,
        ctx: SessionContext | None,
        session_id: str,
        request_id: str,
    ) -> None:
        ask_ids = self._dsh_pending_request_asks.pop(
            (session_id, request_id), set()
        )
        if ctx is None:
            return
        for ask_id in ask_ids:
            future = ctx.pending_asks.get(ask_id)
            if future is not None and not future.done():
                future.set_exception(AskSuperseded())

    @staticmethod
    def _dsh_ask_id(
        session_id: str,
        request_id: str,
        suffix: str,
    ) -> str:
        digest = hashlib.sha256(
            f"{session_id}\0{request_id}\0{suffix}".encode(
                "utf-8", "surrogatepass"
            )
        ).hexdigest()[:32]
        return f"dsh-ask-{digest}"

    async def _answer_dsh_approval(
        self, ctx: SessionContext, frame: dict, approval_key: str,
    ) -> None:
        client = self._dsh_client
        if client is None or not ctx.session_id:
            return
        payload = frame["payload"]
        request_id = frame["rpcId"]
        wire_sid = self._ctx_wire_sid(ctx)
        if not wire_sid:
            raise DshProtocolError("DSH approval session is unavailable")
        tool = payload.get("toolName")
        reason = payload.get("reason")
        question = f"DeepSeek Harness 请求使用工具：{tool or 'unknown'}"
        if isinstance(reason, str) and reason:
            question += f"\n原因：{reason[:4000]}"
        ask_id = self._dsh_ask_id(wire_sid, request_id, approval_key)
        request_key = (wire_sid, request_id)
        self._dsh_pending_request_asks.setdefault(request_key, set()).add(ask_id)
        try:
            answer = await self._on_ask_optional(
                ctx,
                question,
                [
                    {"label": "允许一次", "ds": "仅批准这一次操作"},
                    {"label": "拒绝", "ds": "拒绝这次操作"},
                ],
                header="DSH 权限审批",
                ask_id=ask_id,
            )
            await client.respond(request_id, {
                "sessionId": ctx.session_id,
                "outcome": (
                    "allowed-once" if answer == "允许一次" else "rejected"
                ),
            })
        except AskSkipped:
            # Let DSH's own answerers (the desktop UI) take it, and otherwise
            # fall through to "unavailable" — which is not consent either.
            await client.decline_waterfall(request_id)
        except AskUnavailable:
            # A disconnected browser, timeout, or superseding native surface
            # is never implicit consent.  DSH keeps this RPC pending until a
            # response arrives, so close it explicitly and fail closed.
            await client.respond(request_id, {
                "sessionId": ctx.session_id,
                "outcome": "rejected",
            })
        finally:
            self._dsh_pending_approvals.pop(
                (wire_sid, approval_key), None
            )
            self._dsh_pending_request_asks.pop(request_key, None)

    async def _answer_dsh_questions(
        self, ctx: SessionContext, frame: dict,
    ) -> None:
        client = self._dsh_client
        if client is None or not ctx.session_id:
            return
        payload = frame["payload"]
        request_id = frame["rpcId"]
        wire_sid = self._ctx_wire_sid(ctx)
        if not wire_sid:
            raise DshProtocolError("DSH question session is unavailable")
        request_key = (wire_sid, request_id)
        questions = payload.get("questions")
        if not isinstance(questions, list) or not questions:
            raise DshProtocolError("DSH question request omitted questions")
        if len(questions) > 32:
            await client.respond_error(
                request_id,
                message="Remote question batch exceeds the supported bound",
            )
            return
        answers: list[dict] = []
        try:
            # Keep a DSH batch contiguous while still allowing AnswerQuestion to
            # run on the machine's ordinary command lane.
            async with ctx.ask_lock:
                for index, question in enumerate(questions):
                    if not isinstance(question, dict):
                        raise DshProtocolError("DSH question item is invalid")
                    question_id = question.get("id")
                    prompt = question.get("question")
                    if not isinstance(question_id, str) or not question_id:
                        raise DshProtocolError("DSH question omitted id")
                    if not isinstance(prompt, str) or not prompt:
                        raise DshProtocolError("DSH question omitted text")
                    detail = question.get("detail")
                    if isinstance(detail, str) and detail:
                        prompt = f"{prompt}\n\n{detail}"
                    raw_options = question.get("options")
                    option_rows = raw_options if isinstance(raw_options, list) else []
                    options: list[dict[str, str]] = []
                    for option in option_rows[:ASK_OPTION_MAX_COUNT]:
                        if not isinstance(option, dict):
                            continue
                        label = option.get("label")
                        if not isinstance(label, str) or not label:
                            continue
                        item = {"label": label[:512]}
                        description = option.get("description")
                        if isinstance(description, str) and description:
                            item["ds"] = description[:2048]
                        options.append(item)
                    multi_select = question.get("multiSelect") is True
                    allow_text = len(option_rows) != len(options) or len(options) < 2
                    ask_id = self._dsh_ask_id(
                        wire_sid,
                        request_id,
                        f"{index}:{question_id}",
                    )
                    self._dsh_pending_request_asks.setdefault(
                        request_key, set(),
                    ).add(ask_id)
                    answer = await self._on_ask_locked(
                        ctx,
                        prompt[:16000],
                        options,
                        header=(str(question.get("header"))[:512]
                                if question.get("header") else None),
                        allow_text=allow_text,
                        multi_select=multi_select,
                        ask_id=ask_id,
                    )
                    values = answer if isinstance(answer, list) else [answer]
                    labels = {option["label"] for option in options}
                    selected = [value for value in values if value in labels]
                    custom_values = [value for value in values if value not in labels]
                    answer_row = {"id": question_id, "selected": selected}
                    if custom_values:
                        answer_row["custom"] = "\n".join(custom_values)[:16000]
                    answers.append(answer_row)
            await client.respond(request_id, {
                "sessionId": ctx.session_id,
                "answer": {"answers": answers},
            })
        except AskSkipped:
            await client.decline_waterfall(request_id)
        except AskUnavailable:
            await client.respond_error(
                request_id,
                message="Remote question interaction was cancelled",
            )
        finally:
            self._dsh_pending_request_asks.pop(request_key, None)

    @staticmethod
    def _completion_state(
        snapshot: SessionPresentationSnapshot,
    ) -> CompletionState:
        return CompletionState(
            completion_id=snapshot.completion_id,
            unread=snapshot.completion_unread,
            revision=snapshot.completion_revision,
        )

    async def _handle_acknowledge_completion(self, cmd) -> CompletionState | None:
        """Clear one session's unread completion receipt across all clients.

        The browser acknowledges the completion badge it just displayed; the
        receipt is durable per session, so a delayed acknowledgement for an
        older turn must not clear a newer turn's badge (the store enforces
        that by exact completion id).
        """
        sid = self._resolve_session_alias(getattr(cmd, "sid", None))
        completion_id = getattr(cmd, "completion_id", None)
        if not sid or not isinstance(completion_id, str):
            return None
        store = self._presentation_store_for_engine("dsh")
        if store is None:
            return None
        try:
            snapshot = await asyncio.to_thread(
                store.acknowledge_completion, "dsh", sid, completion_id,
            )
        except SessionPresentationStoreError:
            log.warning(
                "session completion acknowledgement failed",
                session_id=sid,
            )
            return None
        event = self._completion_state(snapshot)
        event.sid = sid
        await self.transport.send(event)
        return event

    def _presentation_store_for_engine(self, engine: str):
        if engine == "codex":
            return self._codex.presentation
        return self._dsh_session_presentation

    def _session_presentation_fields(
        self,
        engine: str,
        session_id: str,
    ) -> dict[str, object]:
        """Project a durable completion receipt into one cold catalog row."""
        store = self._presentation_store_for_engine(engine)
        if store is None:
            return {}
        try:
            snapshot = store.get(engine, session_id)
        except SessionPresentationStoreError:
            log.warning(
                "session completion receipt could not be listed",
                session_id=session_id,
            )
            return {}
        if snapshot.completion_revision == 0:
            return {}
        return {
            "completion_id": snapshot.completion_id,
            "completion_unread": snapshot.completion_unread,
            "completion_revision": snapshot.completion_revision,
        }

    def _history_revision(self, sid: str) -> str:
        return f"{self.instance_id}-{self._history_revision_epochs.get(sid, 0)}"

    def _bump_history_revision(self, sid: str) -> str:
        """Invalidate engine-neutral projections and advance the browser epoch."""
        self._history_revision_epochs[sid] = (
            self._history_revision_epochs.get(sid, 0) + 1
        )
        history_index = getattr(self, "_history_index", None)
        if history_index is not None:
            try:
                history_index.invalidate_session(sid)
            except Exception as exc:
                log.warning(
                    "history index invalidation failed", session_id=sid,
                    error=str(exc),
                )
        return self._history_revision(sid)

    def _focused_ctx(self) -> Optional[SessionContext]:
        return self.sessions.get(self.focused_sid) if self.focused_sid else None

    def _bind_control_revision(self, ctx: SessionContext) -> None:
        """Bind one resident context to the machine-local monotonic sid epoch."""
        key = self._ctx_wire_sid(ctx) or ctx.key
        if not key:
            return
        if ctx.control_revision_key != key:
            previous = self._control_revision_epochs.get(key)
            if previous is not None:
                ctx.control_revision = max(ctx.control_revision, previous + 1)
            ctx.control_revision_key = key
        previous = self._control_revision_epochs.get(key)
        if previous is None or ctx.control_revision > previous:
            self._control_revision_epochs[key] = ctx.control_revision
        elif ctx.control_revision < previous:
            # A bound context must never emit below a revision already published
            # for this sid, even if a caller restored stale in-memory state.
            ctx.control_revision = previous

    def _session_control(self, ctx: SessionContext) -> SessionControl:
        """Build the small authoritative ownership projection for Web."""
        self._bind_control_revision(ctx)
        return SessionControl(
            control_mode=ctx.control_mode,
            write_state=ctx.write_state,
            terminal_attached=ctx.terminal_attached,
            reason=ctx.control_reason,
            can_takeover=ctx.control_can_takeover,
            generation=self.instance_id,
            revision=ctx.control_revision,
        )

    async def _set_session_control(
        self,
        ctx: SessionContext,
        *,
        control_mode: str,
        write_state: str,
        terminal_attached: bool,
        reason: Optional[str] = None,
        can_takeover: bool = False,
        emit: bool = True,
    ) -> SessionControl:
        """Update one control epoch and optionally publish it.

        Revision changes are value-driven, not poll-driven. This keeps repeated
        ownership scans idempotent while letting the browser reject a delayed
        read-only frame from an older process generation.
        """
        values = (
            control_mode,
            write_state,
            bool(terminal_attached),
            reason,
            bool(can_takeover),
        )
        current = (
            ctx.control_mode,
            ctx.write_state,
            ctx.terminal_attached,
            ctx.control_reason,
            ctx.control_can_takeover,
        )
        if values != current:
            ctx.control_mode = control_mode
            ctx.write_state = write_state
            ctx.terminal_attached = bool(terminal_attached)
            ctx.control_reason = reason
            ctx.control_can_takeover = bool(can_takeover)
            self._bind_control_revision(ctx)
            ctx.control_revision += 1
            key = ctx.control_revision_key
            if key:
                self._control_revision_epochs[key] = ctx.control_revision
        snapshot = self._session_control(ctx)
        if emit and values != current:
            await self._emit(ctx, snapshot)
        return snapshot

    def _is_resident_context(self, ctx: SessionContext) -> bool:
        """Whether ``ctx`` still belongs to this machine's resident pool."""
        return ctx.key is not None and self.sessions.get(ctx.key) is ctx

    def _resolve_session_alias(self, sid: Optional[str]) -> Optional[str]:
        if not sid:
            return sid
        alias = self._session_aliases.get(sid)
        if alias is None:
            return sid
        self._session_aliases.move_to_end(sid)
        return alias["session_id"]

    def _alias_file(self):
        return self.cfg.state_dir / "session-aliases.json"

    def _load_session_aliases(self) -> OrderedDict[str, dict]:
        aliases: OrderedDict[str, dict] = OrderedDict()
        try:
            path = self._alias_file()
            if path.stat().st_size > self.SESSION_ALIAS_FILE_MAX_BYTES:
                raise ValueError("session alias state exceeds size limit")
            with path.open() as stream:
                raw_text = stream.read(self.SESSION_ALIAS_FILE_MAX_BYTES + 1)
            if len(raw_text.encode("utf-8", "surrogatepass")) \
                    > self.SESSION_ALIAS_FILE_MAX_BYTES:
                raise ValueError("session alias state exceeds size limit")
            raw = json.loads(raw_text)
            if not isinstance(raw, dict):
                raise ValueError("session alias state has an invalid shape")
            profile_meta = raw.pop(self.SESSION_ALIAS_PROFILE_META_KEY, None)
            if profile_meta is not None:
                if (
                    not isinstance(profile_meta, dict)
                    or set(profile_meta) != {"revision"}
                    or isinstance(profile_meta.get("revision"), bool)
                    or not isinstance(profile_meta.get("revision"), int)
                    or profile_meta["revision"] < 0
                ):
                    raise ValueError(
                        "session alias profile metadata is invalid")
                self._session_alias_profile_revision = profile_meta["revision"]
            now = time.time()
            for old_key, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                real = entry.get("session_id")
                cwd = entry.get("cwd")
                created = entry.get("created_at", 0)
                if (
                    isinstance(old_key, str)
                    and re.fullmatch(r"tmp-[0-9a-f]{32}", old_key)
                    and isinstance(real, str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", real)
                    and isinstance(cwd, str) and "\x00" not in cwd
                    and len(cwd.encode("utf-8", "surrogatepass")) <= 4096
                    and isinstance(created, (int, float))
                    and 0 <= now - created <= self.SESSION_ALIAS_TTL
                ):
                    aliases[old_key] = {
                        "session_id": real, "cwd": cwd, "created_at": created,
                    }
            while len(aliases) > self.SESSION_ALIAS_CAP:
                aliases.popitem(last=False)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("session alias state ignored", error_type=type(exc).__name__)
        return aliases

    def _persist_session_aliases(self) -> None:
        path = self._alias_file()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        payload = OrderedDict(self._session_aliases)
        payload[self.SESSION_ALIAS_PROFILE_META_KEY] = {
            "revision": self._session_alias_profile_revision,
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.SESSION_ALIAS_FILE_MAX_BYTES:
            raise ValueError("session alias state exceeds size limit")
        tmp = path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _remember_session_alias(self, old_key: str, session_id: str, cwd: str) -> None:
        self._session_aliases[old_key] = {
            "session_id": session_id,
            "cwd": cwd,
            "created_at": time.time(),
        }
        self._session_aliases.move_to_end(old_key)
        while len(self._session_aliases) > self.SESSION_ALIAS_CAP:
            self._session_aliases.popitem(last=False)
        try:
            self._persist_session_aliases()
        except Exception as exc:
            log.warning("session alias state not persisted",
                        error_type=type(exc).__name__)

    async def _cancel_dsh_commands_for_shutdown(self) -> None:
        """Cancel command HTTP owners without publishing a user interrupt."""
        tasks: list[asyncio.Task] = []
        for ctx in self.sessions.values():
            task = ctx.turn_task
            if (
                ctx.engine == "dsh"
                and ctx.dsh_pending_command_msg_id is not None
                and task is not None
                and not task.done()
            ):
                ctx.dsh_pending_command_msg_id = None
                ctx.dsh_pending_command_prompt = None
                tasks.append(task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- lifecycle ----

    async def run(self) -> None:
        # Bring up the relay first, then the DSH loopback carrier.
        await self.transport.start()
        self._codex_start_task = asyncio.create_task(self._codex.start())
        try:
            if self._dsh_client is not None:
                await self._dsh_client.start(
                    self._on_dsh_mux,
                    self._on_dsh_host,
                    self._on_dsh_availability,
                )
            await self._on_transport_connected()
            async for cmd in self.transport.incoming():
                if cmd.type == "list_sessions":
                    self._start_session_list_command(cmd)
                    continue
                if cmd.type in {"get_history", "get_turn_detail"}:
                    self._start_history_command(cmd)
                    continue
                if cmd.type in {"get_models", "get_account_usage"}:
                    self._start_models_command(cmd)
                    continue
                if cmd.type == "steer":
                    # turn/steer is a short DSH RPC, but it must not
                    # monopolize the serial command lane and delay an explicit
                    # Stop arriving from another client.
                    self._start_interactive_control_command(cmd)
                    continue
                if cmd.type == "get_engine_capabilities":
                    self._start_capabilities_command(cmd)
                    continue
                await self._process_command_safely(cmd)

        finally:
            if self._codex_start_task:
                self._codex_start_task.cancel()
                await asyncio.gather(self._codex_start_task, return_exceptions=True)
            await self._codex.stop()
            await self._cancel_dsh_commands_for_shutdown()
            tasks = set(self._dsh_interaction_tasks)
            for registry in (self._models_command_tasks, self._status_command_tasks,
                             self._capabilities_command_tasks, self._interactive_control_tasks,
                             self._history_command_tasks, self._history_page_tasks,
                             self._history_refresh_tasks):
                tasks.update(registry.values())
                registry.clear()
            tasks.update(self._session_list_command_tasks)
            if self._watch_task:
                tasks.add(self._watch_task)
            for ctx in self.sessions.values():
                for task in (ctx.turn_task, ctx.queued_query_drain_task, ctx.codex_spontaneous_task):
                    if task is not None:
                        tasks.add(task)
            current = asyncio.current_task()
            tasks.discard(current)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._dsh_client is not None:
                await self._dsh_client.stop()
            await self.transport.stop()

    async def _renew_work_schedule_lease(self, store, run_id: str) -> None:
        while True:
            await asyncio.sleep(30)
            renewed = await asyncio.to_thread(
                store.renew_schedule_run, run_id, time.time()
            )
            if not renewed:
                return

    async def _on_transport_connected(self) -> None:
        ctx = self._focused_ctx()
        await self.transport.send(Hello(
            role="wrapper",
            machine_id=self.cfg.machine_id,
            wrapper_generation=self.instance_id,
            cc_session_id=(self._ctx_wire_sid(ctx) if ctx else None),
            state=(ctx.state if ctx else "idle"),
            buffer_head_seq=(ctx.buffer.head_seq if ctx else 0),
            buffer_tail_seq=(ctx.buffer.tail_seq if ctx else 0),
        ))
        await self.transport.send(self._engine_catalog())

    @staticmethod
    def _path_is_below(root: str, candidate: str) -> bool:
        try:
            return os.path.commonpath((root, candidate)) == root
        except ValueError:
            return False

    def _normalize_preview_write_event(self, msg) -> tuple[str, ...]:
        """Decorate one write ToolUse without granting filesystem access.

        Historical translation also calls this presentation-only helper. It must
        never populate the live pending map: a transcript records the old path,
        but not the device/inode observed when that write completed.
        """
        if (
            not isinstance(msg, ToolUse)
            or (msg.tool or "").lower() not in self.PREVIEW_WRITE_TOOLS
        ):
            return ()
        if not raw_paths:
            return ()
        # ``file_paths`` is the engine-neutral presentation field. Keep the
        # original payload beside it so old clients and detailed tool views
        # retain the exact SDK/app-server input.
        if msg.input.get("file_paths") != list(raw_paths):
            msg.input = dict(msg.input)
            msg.input["file_paths"] = list(raw_paths)
        return raw_paths

    def _preview_image_read_path(msg: ToolUse) -> Optional[str]:
        """Return the exact path for a built-in image-reading tool."""
        tool = re.sub(r"[^a-z0-9]", "", (msg.tool or "").lower())
        if tool not in {"read", "readfile", "viewimage"}:
            return None
        for key in ("file_path", "path"):
            candidate = msg.input.get(key)
            if (
                isinstance(candidate, str)
                and candidate
                and not candidate.startswith("~")
                and len(candidate) <= 4096
            ):
                return candidate
        return None

    def _observe_active_turn_binding(
        self, ctx: SessionContext, msg,
    ) -> None:
        """Track only authoritative live owner boundaries emitted on the wire."""
        if isinstance(msg, (TurnBinding, TurnSteered)):
            if msg.seq is None:
                return
            ctx.active_turn_binding = ActiveTurnBinding(
                msg_id=msg.msg_id,
                turn_id=msg.turn_id,
                seq=msg.seq,
                generation=self.instance_id,
            )
            return
        if isinstance(msg, TurnEnd) or (
            isinstance(msg, StateEvent) and msg.state == "idle"
        ):
            ctx.active_turn_binding = None

    def _ctx_wire_sid(self, ctx: SessionContext) -> Optional[str]:
        if ctx.key:
            return ctx.key
        return ctx.session_id
    def _ctx_by_sid(self, sid: str) -> Optional[SessionContext]:
        sid = self._resolve_session_alias(sid) or sid
        exact = self.sessions.get(sid)
        if exact is not None:
            return exact
        # Legacy/internal callers sometimes still hold a native DSH id.
        return next((
            ctx for ctx in self.sessions.values()
            if ctx.session_id == sid
        ), None)

    def _ctx_for(self, sid: Optional[str]) -> Optional[SessionContext]:
        """Resolve a command's target ctx. An EXPLICIT sid that isn't resident
        returns None — NOT the focused ctx: a session whose spawn failed (e.g. bad
        codex config) must not silently reroute its query to whatever is focused
        (that made a failed session look like "no response" while its "在？" landed
        on another session). Only an absent sid (legacy/untagged commands) falls
        back to the focused view. Iterates a snapshot (a turn may re-key mid-scan)."""
        if sid:
            sid = self._resolve_session_alias(sid)
            ctx = self.sessions.get(sid)
            if ctx:
                return ctx
            for c in list(self.sessions.values()):
                if c.session_id != sid:
                    continue
                if c.engine != "codex":
                    return c
            return None
        return self._focused_ctx()

    async def _drain_query_queue(self, ctx: SessionContext) -> None:
        """Start wrapper-owned queries in order without any browser callback."""
        current_task = asyncio.current_task()
        retry_delay = 1.0
        cancelled = False
        try:
            while ctx.queued_queries and self._is_resident_context(ctx):
                ctx.queued_query_wakeup.clear()
                async with ctx.queued_query_lock:
                    head = ctx.queued_queries[0] if ctx.queued_queries else None
                    lease = ctx.queued_query_editors.get(head.msg_id) if head else None
                    edit_wait = max(0, lease[1] - time.monotonic()) if lease else 0
                if edit_wait:
                    try:
                        await asyncio.wait_for(ctx.queued_query_wakeup.wait(), edit_wait)
                    except asyncio.TimeoutError:
                        pass
                    continue
                active = next((
                    task for task in (
                        ctx.turn_task,
                        ctx.codex_spontaneous_task,
                    )
                    if task is not None and not task.done()
                    and task is not current_task
                ), None)
                if active is not None or ctx.state != "idle":
                    ctx.queued_query_wakeup.clear()
                    if active is not None:
                        active.add_done_callback(
                            lambda _done: ctx.queued_query_wakeup.set())
                    # Close the clear/recheck race with a terminal state or task
                    # completion that happened immediately before the callback.
                    if (
                        ctx.state == "idle"
                        and not any(
                            task is not None and not task.done()
                            for task in (
                                ctx.turn_task,
                                ctx.codex_spontaneous_task,
                            )
                        )
                    ):
                        ctx.queued_query_wakeup.set()
                    await ctx.queued_query_wakeup.wait()
                    retry_delay = 1.0
                    continue

                command = None
                result = None
                launch_error: str | None = None
                # Any meaningful queue/state mutation after this point requests
                # another preflight.  If this attempt fails and nobody changes
                # anything, wait instead of emitting the same error in a loop.
                ctx.queued_query_wakeup.clear()
                try:
                    # The browser command lane and this worker share query_lock:
                    # once an item starts preflight, no later immediate query can
                    # claim the same idle boundary first.  The item deliberately
                    # remains queued until every synchronous rejection path has
                    # passed, so a daemon/ownership failure cannot drop work.
                    async with ctx.query_lock:
                        active_now = any(
                            task is not None and not task.done()
                            for task in (
                                ctx.turn_task,
                                ctx.codex_spontaneous_task,
                            )
                        )
                        if ctx.state != "idle" or active_now:
                            continue
                        edit_wait = None
                        async with ctx.queued_query_lock:
                            if ctx.queued_queries:
                                head = ctx.queued_queries[0]
                                lease = ctx.queued_query_editors.get(head.msg_id)
                                if lease and lease[1] > time.monotonic():
                                    edit_wait = lease[1] - time.monotonic()
                                else:
                                    ctx.queued_query_editors.pop(head.msg_id, None)
                                    command = head
                                    ctx.queued_query_starting_msg_id = command.msg_id
                        if edit_wait is not None:
                            continue
                        if command is not None:
                            immediate = command.model_copy(
                                deep=True, update={"delivery": "immediate"})
                            result = await self._handle_immediate_query(
                                ctx, immediate)
                            if not isinstance(result, Error):
                                async with ctx.emit_lock:
                                    async with ctx.queued_query_lock:
                                        index = next((
                                            index for index, queued
                                            in enumerate(ctx.queued_queries)
                                            if queued.msg_id == command.msg_id
                                        ), None)
                                        if index is not None:
                                            removed = ctx.queued_queries.pop(
                                                index)
                                            size = self._queued_query_size(
                                                removed)
                                            ctx.queued_query_bytes -= size
                                            self._queued_query_count -= 1
                                            self._queued_query_bytes -= size
                                        ctx.queued_query_errors.pop(
                                            command.msg_id, None)
                                        if (
                                            ctx.queued_query_starting_msg_id
                                            == command.msg_id
                                        ):
                                            ctx.queued_query_starting_msg_id = (
                                                None)
                                        try:
                                            await self._emit_locked(
                                                ctx,
                                                self._query_queue_state(ctx),
                                            )
                                        except Exception as exc:
                                            log.warning(
                                                "query queue start projection "
                                                "delayed",
                                                session_id=(
                                                    self._ctx_wire_sid(ctx)
                                                ),
                                                error_type=type(exc).__name__,
                                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if command is None:
                        raise
                    log.exception(
                        "deferred query launch failed",
                        session_id=self._ctx_wire_sid(ctx),
                        msg_id=command.msg_id,
                        error_type=type(exc).__name__,
                    )
                    launch_error = "排队消息启动失败；消息仍在队列中，请重试。"
                    try:
                        await self._emit(ctx, Error(
                            code=ERR_INTERNAL,
                            message=launch_error,
                            msg_id=command.msg_id,
                        ))
                    except Exception as emit_exc:
                        log.warning(
                            "deferred query launch error delivery delayed",
                            session_id=self._ctx_wire_sid(ctx),
                            error_type=type(emit_exc).__name__,
                        )
                if command is None:
                    continue
                if isinstance(result, Error):
                    launch_error = result.message
                if launch_error is not None:
                    async with ctx.emit_lock:
                        async with ctx.queued_query_lock:
                            if any(
                                queued.msg_id == command.msg_id
                                for queued in ctx.queued_queries
                            ):
                                ctx.queued_query_errors[
                                    command.msg_id] = launch_error
                            if (
                                ctx.queued_query_starting_msg_id
                                == command.msg_id
                            ):
                                ctx.queued_query_starting_msg_id = None
                            try:
                                await self._emit_locked(
                                    ctx, self._query_queue_state(ctx))
                            except Exception as exc:
                                log.warning(
                                    "query queue failure projection delayed",
                                    session_id=self._ctx_wire_sid(ctx),
                                    error_type=type(exc).__name__,
                                )
                    try:
                        await asyncio.wait_for(
                            ctx.queued_query_wakeup.wait(),
                            timeout=retry_delay,
                        )
                        retry_delay = 1.0
                    except asyncio.TimeoutError:
                        # A daemon/control transition can become usable without
                        # a browser command or lifecycle state change. Keep an
                        # autonomous, bounded-backoff retry path for sleeping
                        # clients while avoiding a hot rejection loop.
                        retry_delay = min(retry_delay * 2, 30.0)
                    continue
                log.info(
                    "deferred query started",
                    session_id=self._ctx_wire_sid(ctx),
                    msg_id=command.msg_id,
                )
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            log.exception(
                "query queue drain stopped unexpectedly",
                session_id=self._ctx_wire_sid(ctx),
            )
        finally:
            if ctx.queued_query_drain_task is current_task:
                ctx.queued_query_drain_task = None
            if ctx.queued_query_starting_msg_id is not None:
                ctx.queued_query_starting_msg_id = None
            if (
                not cancelled
                and ctx.queued_queries
                and self._is_resident_context(ctx)
            ):
                self._schedule_query_queue_drain(ctx)

    async def _on_ask_locked(
        self,
        ctx: SessionContext,
        question: str,
        options: list[dict[str, str]],
        *,
        header: str | None = None,
        allow_text: bool = False,
        secret: bool = False,
        multi_select: bool = False,
        timeout: float = 30 * 60,
        ask_id: str | None = None,
        to: str | None = None,
    ) -> str | list[str]:
        """Run one question while the caller owns ``ctx.ask_lock``."""
        # ask_id is an identity, not a downstream sequence.  Consuming next_seq
        # here would leave an invisible hole before _emit assigns AskUser.seq;
        # reconnect replay would then appear to have lost a frame.
        ask_id = ask_id or f"ask-{uuid4().hex}"
        # Validate model-originated text before registering a pending Future.
        # Otherwise a malformed/oversized AskUser raises during emit and leaves
        # an unreachable entry in pending_asks for the life of the session.
        event = AskUser(ask_id=ask_id, question=question, options=options,
                        header=header, allow_text=allow_text, secret=secret,
                        multi_select=multi_select, to=to)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        ctx.pending_asks[ask_id] = fut
        ctx.pending_ask_specs[ask_id] = {
            "labels": frozenset(option["label"] for option in options),
            "allow_text": allow_text,
            "multi_select": multi_select,
            # Kept so a client that reloads while this prompt is still open can
            # be shown it again instead of leaving the agent blocked invisibly.
            "event": event,
        }
        reason = "cancelled"
        try:
            await self._emit(ctx, event)
            log.info(
                "ask_user emitted",
                sid=ctx.session_id,
                ask_id=ask_id,
                options=len(options),
                multi_select=multi_select,
            )
            answer = await asyncio.wait_for(fut, timeout=timeout)
            reason = "answered"
            return answer
        except asyncio.TimeoutError:
            reason = "timeout"
            log.warning("ask_user timed out", ask_id=ask_id)
            raise AskTimeout from None
        except AskSuperseded:
            reason = "superseded"
            raise
        except AskSkipped:
            # The user dismissed the prompt: close the card everywhere, and let
            # DSH decide what an unanswered request means.
            reason = "cancelled"
            raise
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        finally:
            if ctx.pending_asks.get(ask_id) is fut:
                ctx.pending_asks.pop(ask_id, None)
                ctx.pending_ask_specs.pop(ask_id, None)
            try:
                await self._emit(
                    ctx,
                    AskUserClosed(ask_id=ask_id, reason=reason, to=to),
                )
            except Exception as exc:
                log.warning(
                    "ask_user close event delayed",
                    ask_id=ask_id,
                    reason=reason,
                    error_type=type(exc).__name__,
                )

    async def _fork_entry_for_cached_command(
        self, cmd, cached_responses: tuple[object, ...],
    ) -> Optional[dict]:
        """Resolve the one journal entry owned by a cached fork command.

        Request ids are browser-generated and therefore cannot be treated as a
        cross-engine namespace.  Match the command's parent/target and its
        cached child before consulting a deletion tombstone; an unrelated
        journal collision must never suppress or resurrect this response.
        """
        request_id = getattr(cmd, "request_id", None)
        requested_parent = getattr(cmd, "session_id", None)
        if not isinstance(request_id, str) or not isinstance(
            requested_parent, str
        ):
            return None
        resolved_parent = (
            self._resolve_session_alias(requested_parent) or requested_parent)
        parents = {requested_parent, resolved_parent}
        target = (
            "worktree" if cmd.type == "fork_session_worktree" else "same_cwd")
        cached = next((
            response for response in cached_responses
            if isinstance(response, SessionForked)
            and response.request_id == request_id
            and response.parent_session_id in parents
            and response.target == target
        ), None)
        child = getattr(cached, "session_id", None)
        if self._dsh_forks is None:
            return None
        try:
            entry = await asyncio.to_thread(
                self._dsh_forks.get, request_id)
        except DshForkJournalError:
            return None
        if not entry:
            return None
        if entry.get("parent_session_id") not in parents:
            return None
        if entry.get("target", "same_cwd") != target:
            return None
        if isinstance(child, str) and entry.get("session_id") != child:
            return None
        return entry

    def _purge_preview_image_snapshots(self, token: str) -> None:
        cache = self._preview_image_snapshots
        for key in tuple(cache):
            if key[0] != token:
                continue
            path, _, payload = cache.pop(key)
            alias_key = (key[0], path)
            if self._preview_image_path_aliases.get(alias_key) == key[1]:
                self._preview_image_path_aliases.pop(alias_key, None)
            self._preview_image_snapshot_bytes -= len(payload)
        for alias_key in tuple(self._preview_image_path_aliases):
            if alias_key[0] == token:
                self._preview_image_path_aliases.pop(alias_key, None)

    async def _emit_locked(self, ctx: SessionContext, msg) -> None:
        # Stamp routing before buffering so byte accounting includes the final
        # wire shape.
        msg.sid = self._ctx_wire_sid(ctx) or ctx.key
        if ctx.btw:
            if not ctx.owner_client_id:
                log.error("dropping frame for ownerless btw", sid=ctx.key,
                          type=getattr(msg, "type", None))
                return
            msg.to = ctx.owner_client_id
        if is_downstream(msg):
            msg.seq = ctx.next_seq()
            ctx.buffer.append(msg)
        if isinstance(msg, TurnEnd):
            # The replayable object is deliberately notification-free. Only a
            # copy sent on this live call receives presentation metadata.
            msg.notification_context = None
        live = (
            msg.model_copy(update={
                "notification_context": self._notification_context(ctx),
            })
            if isinstance(msg, TurnEnd)
            else msg
        )
        await self.transport.send(live)


    async def _emit(self, ctx: SessionContext, msg) -> None:
        async with ctx.emit_lock:
            await self._emit_locked(ctx, msg)
            presentation_store = self._presentation_store_for_engine(
                ctx.engine
            )
            if (
                isinstance(msg, TurnEnd)
                and not msg.result.is_error
                and not ctx.btw
                and presentation_store is not None
            ):
                sid = self._ctx_wire_sid(ctx) or ctx.key
                if sid:
                    try:
                        snapshot = await asyncio.to_thread(
                            presentation_store.mark_completion,
                            ctx.engine,
                            sid,
                            msg.turn_id,
                        )
                    except SessionPresentationStoreError:
                        log.warning(
                            "session completion receipt could not be persisted",
                            session_id=sid,
                        )
                    else:
                        # Publish after TurnEnd so every client first observes
                        # the real terminal boundary, then the shared unread
                        # receipt which a visible client may acknowledge.
                        await self._emit_locked(
                            ctx, self._completion_state(snapshot)
                        )

    async def _emit_focused(self, msg) -> None:
        """For control-path errors with no target ctx (cap reached, bad cwd):
        route to the focused ctx if any, else send unbuffered."""
        ctx = self._focused_ctx()
        if ctx is not None:
            await self._emit(ctx, msg)
        else:
            msg.sid = self.focused_sid
            await self.transport.send(msg)

    async def _emit_to_sid(self, sid: Optional[str], msg) -> None:
        """Route a control-path frame to a SPECIFIC session's view (tagged by sid),
        even when it has no ctx — so a failed-to-spawn session's error surfaces on
        that session, not on whatever happens to be focused."""
        ctx = self.sessions.get(sid) if sid else None
        if ctx is not None:
            await self._emit(ctx, msg)
        else:
            msg.sid = sid or self.focused_sid
            await self.transport.send(msg)

    async def _missing_session_error(self, cmd, action: str) -> Error:
        """Reject a routed command whose session is no longer resident.

        Reliable commands are ACKed after their handler returns.  Returning a
        targeted Error here prevents an expired sid from becoming an ACK-only
        false success and keeps the failure out of an unrelated focused view.
        """
        sid = getattr(cmd, "sid", None)
        error = Error(
            code=ERR_NOT_RUNNING,
            message=f"该会话未启动，无法{action}",
            request_id=getattr(cmd, "cmd_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self._emit_to_sid(sid, error)
        return error

    @staticmethod
    def _queued_query_size(cmd: Query) -> int:
        """Bound retained payloads without serializing another giant JSON copy."""
        size = 128 + len(cmd.prompt.encode("utf-8", "surrogatepass"))
        for image in cmd.images or ():
            size += (
                64
                + len(image.get("media_type", "").encode("utf-8"))
                + len(image.get("data", ""))
            )
        for file in cmd.files or ():
            size += (
                64
                + len(file.get("filename", "").encode(
                    "utf-8", "surrogatepass"))
                + len(file.get("data", ""))
            )
        return size

    @staticmethod
    def _query_queue_task_active(ctx: SessionContext) -> bool:
        task = ctx.queued_query_drain_task
        return task is not None and not task.done()

    def _queued_query_info(
        self, ctx: SessionContext, cmd: Query,
    ) -> QueuedQueryInfo:
        return QueuedQueryInfo(
            msg_id=cmd.msg_id,
            kind="replace" if cmd.delivery == "replace" else "queue",
            prompt_preview=cmd.prompt[:512],
            image_count=len(cmd.images or ()),
            file_count=len(cmd.files or ()),
            retained_bytes=self._queued_query_size(cmd),
            error=ctx.queued_query_errors.get(cmd.msg_id),
        )

    def _query_queue_state(self, ctx: SessionContext) -> QueryQueueState:
        return QueryQueueState(
            can_start_now=ctx.engine in {"dsh", "codex"},
            items=[
                self._queued_query_info(ctx, command)
                for command in ctx.queued_queries
            ],
            total_count=self._queued_query_count,
            total_bytes=self._queued_query_bytes,
        )

    async def _emit_deferred_query_error(
        self,
        ctx: SessionContext,
        cmd: Query,
        code: str,
        message: str,
    ) -> Error:
        error = Error(
            code=code,
            message=message,
            msg_id=cmd.msg_id,
            request_id=getattr(cmd, "cmd_id", None),
            to=getattr(cmd, "client_id", None),
            sid=self._ctx_wire_sid(ctx),
        )
        # A rejected enqueue is private command feedback, not shared session
        # narrative. Reliable-command retry caches it for the origin; buffering
        # it in the session ring would expose it to other clients on Hello.
        await self.transport.send(error)
        # A rejected replacement may have optimistically hidden the previous
        # server-owned replacement in the browser. Re-publish the unchanged
        # authoritative projection after the private error so it is restored
        # without waiting for another reconnect or queue mutation.
        async with ctx.emit_lock:
            async with ctx.queued_query_lock:
                try:
                    await self._emit_locked(
                        ctx, self._query_queue_state(ctx))
                except Exception as exc:
                    log.warning(
                        "query queue rejection projection delayed",
                        session_id=self._ctx_wire_sid(ctx),
                        error_type=type(exc).__name__,
                    )
        return error

    async def _enqueue_deferred_query(
        self, ctx: SessionContext, cmd: Query,
    ) -> Error | None:
        """Transfer one append/replace query to the wrapper-owned bounded FIFO."""
        if not cmd.prompt and not cmd.images and not cmd.files:
            return await self._emit_deferred_query_error(
                ctx, cmd, ERR_BAD_PROMPT,
                "消息内容为空，请输入内容或添加附件。",
            )
        attachment_error = validate_attachments(cmd.images, cmd.files)
        if attachment_error:
            return await self._emit_deferred_query_error(
                ctx, cmd, ERR_BAD_PROMPT,
                "附件不符合要求，请调整后重试。",
            )
        if ctx.write_state != "writable":
            return await self._emit_deferred_query_error(
                ctx, cmd, ERR_BUSY,
                "该会话当前不可写，本次排队未提交。",
            )

        size = self._queued_query_size(cmd)
        accepted = False
        async with ctx.emit_lock:
            async with ctx.queued_query_lock:
                duplicate = next((
                    queued for queued in ctx.queued_queries
                    if queued.msg_id == cmd.msg_id
                ), None)
                if duplicate is not None:
                    # An ACK-lost replay is idempotent. Re-publish the current
                    # projection so the origin can recover without another turn.
                    state = self._query_queue_state(ctx)
                else:
                    replace_index = next((
                        index for index, queued in enumerate(ctx.queued_queries)
                        if queued.delivery == "replace"
                    ), None) if cmd.delivery == "replace" else None
                    replaced = (
                        ctx.queued_queries[replace_index]
                        if replace_index is not None else None
                    )
                    if (
                        replaced is not None
                        and replaced.msg_id
                        == ctx.queued_query_starting_msg_id
                    ):
                        error = Error(
                            code=ERR_BUSY,
                            message=(
                                "上一条替换消息正在启动，本次排队未提交；"
                                "请稍后重试。"
                            ),
                            msg_id=cmd.msg_id,
                            request_id=getattr(cmd, "cmd_id", None),
                            to=getattr(cmd, "client_id", None),
                            sid=self._ctx_wire_sid(ctx),
                        )
                        try:
                            await self.transport.send(error)
                        except Exception as exc:
                            log.warning(
                                "deferred query rejection delivery delayed",
                                session_id=self._ctx_wire_sid(ctx),
                                error_type=type(exc).__name__,
                            )
                        try:
                            await self._emit_locked(
                                ctx, self._query_queue_state(ctx))
                        except Exception as exc:
                            log.warning(
                                "query queue rejection projection delayed",
                                session_id=self._ctx_wire_sid(ctx),
                                error_type=type(exc).__name__,
                            )
                        return error
                    replaced_size = (
                        self._queued_query_size(replaced)
                        if replaced is not None else 0
                    )
                    projected_count = (
                        self._queued_query_count
                        - (1 if replaced is not None else 0)
                        + 1
                    )
                    projected_bytes = (
                        self._queued_query_bytes - replaced_size + size
                    )
                    if (
                        projected_count > MAX_QUERY_QUEUE_ITEMS
                        or projected_bytes > MAX_QUERY_QUEUE_BYTES
                    ):
                        error = Error(
                            code=ERR_QUEUE_FULL,
                            message=(
                                "服务端排队已满（最多 32 条 / 64 MiB），"
                                "请等待已有任务开始后重试。"
                            ),
                            msg_id=cmd.msg_id,
                            request_id=getattr(cmd, "cmd_id", None),
                            to=getattr(cmd, "client_id", None),
                            sid=self._ctx_wire_sid(ctx),
                        )
                        try:
                            await self.transport.send(error)
                        except Exception as exc:
                            log.warning(
                                "deferred query rejection delivery delayed",
                                session_id=self._ctx_wire_sid(ctx),
                                error_type=type(exc).__name__,
                            )
                        try:
                            await self._emit_locked(
                                ctx, self._query_queue_state(ctx))
                        except Exception as exc:
                            log.warning(
                                "query queue rejection projection delayed",
                                session_id=self._ctx_wire_sid(ctx),
                                error_type=type(exc).__name__,
                            )
                        return error

                    if replace_index is not None:
                        ctx.queued_queries.pop(replace_index)
                        ctx.queued_query_errors.pop(replaced.msg_id, None)
                        ctx.queued_query_bytes -= replaced_size
                        self._queued_query_count -= 1
                        self._queued_query_bytes -= replaced_size
                    if cmd.delivery == "replace":
                        ctx.queued_queries.insert(0, cmd)
                    else:
                        ctx.queued_queries.append(cmd)
                    ctx.queued_query_bytes += size
                    self._queued_query_count += 1
                    self._queued_query_bytes += size
                    state = self._query_queue_state(ctx)
                    accepted = True
                try:
                    await self._emit_locked(ctx, state)
                except Exception as exc:
                    # The replayable state is already in the ring. Queue
                    # ownership and execution must not depend on a live browser.
                    log.warning(
                        "query queue live projection delayed",
                        session_id=self._ctx_wire_sid(ctx),
                        error_type=type(exc).__name__,
                    )

        if accepted:
            log.info(
                "deferred query accepted",
                session_id=self._ctx_wire_sid(ctx),
                msg_id=cmd.msg_id,
                delivery=cmd.delivery,
                queued=len(ctx.queued_queries),
            )
        self._schedule_query_queue_drain(ctx)
        return None

    async def _handle_cancel_queued_query(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "取消排队消息")
        removed = None
        async with ctx.emit_lock:
            async with ctx.queued_query_lock:
                index = next((
                    index for index, queued in enumerate(ctx.queued_queries)
                    if queued.msg_id == cmd.msg_id
                ), None)
                if index is not None:
                    candidate = ctx.queued_queries[index]
                    if (
                        candidate.msg_id
                        != ctx.queued_query_starting_msg_id
                    ):
                        removed = ctx.queued_queries.pop(index)
                        ctx.queued_query_errors.pop(removed.msg_id, None)
                        ctx.queued_query_editors.pop(removed.msg_id, None)
                        size = self._queued_query_size(removed)
                        ctx.queued_query_bytes -= size
                        self._queued_query_count -= 1
                        self._queued_query_bytes -= size
                try:
                    await self._emit_locked(
                        ctx, self._query_queue_state(ctx))
                except Exception as exc:
                    log.warning(
                        "query queue cancellation projection delayed",
                        session_id=self._ctx_wire_sid(ctx),
                        error_type=type(exc).__name__,
                    )
        if removed is not None:
            ctx.queued_query_wakeup.set()
        if removed is not None:
            log.info(
                "deferred query cancelled",
                session_id=self._ctx_wire_sid(ctx),
                msg_id=cmd.msg_id,
            )

    async def _handle_get_queued_query(self, cmd) -> QueuedQueryDetail:
        """Return one full prompt privately without buffering queue payloads."""
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            detail = QueuedQueryDetail(
                sid=cmd.sid,
                msg_id=cmd.msg_id,
                request_id=cmd.cmd_id,
                error="排队消息所在会话已不在运行。",
                to=cmd.client_id,
            )
        else:
            async with ctx.queued_query_lock:
                queued = next((
                    candidate for candidate in ctx.queued_queries
                    if candidate.msg_id == cmd.msg_id
                ), None)
                edit_error = None
                action = getattr(cmd, "edit_action", None)
                lease = ctx.queued_query_editors.get(cmd.msg_id)
                if lease and lease[1] <= time.monotonic():
                    ctx.queued_query_editors.pop(cmd.msg_id, None)
                    lease = None
                if queued is not None and action:
                    if queued.msg_id == ctx.queued_query_starting_msg_id:
                        edit_error = "该消息正在启动，暂时无法编辑。"
                    elif lease and lease[0] != cmd.client_id:
                        edit_error = "另一端正在编辑这条消息，请稍后重试。"
                    elif action == "end":
                        ctx.queued_query_editors.pop(cmd.msg_id, None)
                        ctx.queued_query_wakeup.set()
                    elif action == "renew" and lease is None:
                        edit_error = "编辑已过期，请关闭后重新打开。"
                    else:
                        ctx.queued_query_editors[cmd.msg_id] = (cmd.client_id, time.monotonic() + 90)
                if queued is None or edit_error:
                    detail = QueuedQueryDetail(
                        sid=self._ctx_wire_sid(ctx),
                        msg_id=cmd.msg_id,
                        request_id=cmd.cmd_id,
                        error=edit_error or "该消息已开始执行或已从队列移除。",
                        to=cmd.client_id,
                    )
                else:
                    detail = QueuedQueryDetail(
                        sid=self._ctx_wire_sid(ctx),
                        msg_id=cmd.msg_id,
                        request_id=cmd.cmd_id,
                        prompt=queued.prompt,
                        kind=(
                            "replace"
                            if queued.delivery == "replace"
                            else "queue"
                        ),
                        images=queued.images,
                        files=queued.files,
                        image_count=len(queued.images or ()),
                        file_count=len(queued.files or ()),
                        error=ctx.queued_query_errors.get(queued.msg_id),
                        to=cmd.client_id,
                    )
        await self.transport.send(detail)
        return detail

    async def _handle_update_queued_query(self, cmd) -> QueuedQueryUpdated:
        """Atomically edit one queued prompt while preserving its attachments."""
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            result = QueuedQueryUpdated(
                sid=cmd.sid,
                msg_id=cmd.msg_id,
                request_id=cmd.cmd_id,
                updated=False,
                error="排队消息所在会话已不在运行。",
                to=cmd.client_id,
            )
            await self.transport.send(result)
            return result

        updated = False
        error = None
        start_now = getattr(cmd, "start_now", False) is True
        if start_now and ctx.engine not in {"dsh", "codex"}:
            result = QueuedQueryUpdated(sid=self._ctx_wire_sid(ctx), msg_id=cmd.msg_id,
                request_id=cmd.cmd_id, updated=False, error="当前设备不支持立即处理。", to=cmd.client_id)
            await self.transport.send(result)
            return result
        async with ctx.emit_lock:
            async with ctx.queued_query_lock:
                index = next((
                    index for index, queued in enumerate(ctx.queued_queries)
                    if queued.msg_id == cmd.msg_id
                ), None)
                if index is None:
                    error = "该消息已开始执行或已从队列移除。"
                elif (
                    ctx.queued_queries[index].msg_id
                    == ctx.queued_query_starting_msg_id
                ):
                    error = "该消息正在启动，请等待本次启动结果后再编辑。"
                elif (ctx.queued_query_editors.get(cmd.msg_id)
                      and ctx.queued_query_editors[cmd.msg_id][1] > time.monotonic()
                      and ctx.queued_query_editors[cmd.msg_id][0] != cmd.client_id):
                    error = "另一端正在编辑这条消息，请稍后重试。"
                else:
                    previous = ctx.queued_queries[index]
                    images = [*(previous.images or []), *(getattr(cmd, "add_images", None) or [])]
                    files = [*(previous.files or []), *(getattr(cmd, "add_files", None) or [])]
                    if (getattr(cmd, "add_images", None) or getattr(cmd, "add_files", None)) and validate_attachments(images, files):
                        error = "附件不符合要求，请检查数量、格式和大小后重试。"
                    elif not cmd.prompt and not images and not files:
                        error = "消息内容为空，请输入内容后再保存。"
                    else:
                        replacement = previous.model_copy(
                            deep=True, update={"prompt": cmd.prompt, "images": images or None, "files": files or None})
                        previous_size = self._queued_query_size(previous)
                        replacement_size = self._queued_query_size(replacement)
                        projected_bytes = (
                            self._queued_query_bytes
                            - previous_size
                            + replacement_size
                        )
                        if projected_bytes > MAX_QUERY_QUEUE_BYTES:
                            error = (
                                "编辑后的服务端队列超过 64 MiB，"
                                "请缩短消息后重试。"
                            )
                        else:
                            ctx.queued_queries[index] = replacement
                            if start_now:
                                # Preserve the same message/attachments and all
                                # other items. The idle-boundary drainer remains
                                # the only path that starts this instruction.
                                ctx.queued_queries.insert(0, ctx.queued_queries.pop(index))
                            ctx.queued_query_errors.pop(
                                previous.msg_id, None)
                            ctx.queued_query_bytes += (
                                replacement_size - previous_size)
                            self._queued_query_bytes = projected_bytes
                            updated = True
                            ctx.queued_query_editors.pop(cmd.msg_id, None)
                            try:
                                await self._emit_locked(
                                    ctx, self._query_queue_state(ctx))
                            except Exception as exc:
                                # The authoritative projection is already in
                                # the replay ring. Editing must not depend on a
                                # currently connected browser.
                                log.warning(
                                    "query queue edit projection delayed",
                                    session_id=self._ctx_wire_sid(ctx),
                                    error_type=type(exc).__name__,
                                )

        if updated and start_now and ctx.state == "running":
            await self._handle_interrupt(cmd)
        result = QueuedQueryUpdated(
            sid=self._ctx_wire_sid(ctx),
            msg_id=cmd.msg_id,
            request_id=cmd.cmd_id,
            updated=updated,
            error=error,
            to=cmd.client_id,
        )
        await self.transport.send(result)
        if updated:
            log.info(
                "deferred query updated",
                session_id=self._ctx_wire_sid(ctx),
                msg_id=cmd.msg_id,
            )
            self._schedule_query_queue_drain(ctx)
        return result

    def _schedule_query_queue_drain(self, ctx: SessionContext) -> None:
        if not ctx.queued_queries or not self._is_resident_context(ctx):
            return
        current = ctx.queued_query_drain_task
        if current is not None and not current.done():
            ctx.queued_query_wakeup.set()
            return
        task = asyncio.create_task(
            self._drain_query_queue(ctx),
            name=f"query-queue-{self._ctx_wire_sid(ctx)}",
        )
        ctx.queued_query_drain_task = task

    async def _discard_query_queue(
        self, ctx: SessionContext, *, publish: bool = False,
    ) -> None:
        """Release bounded queue accounting before a resident is destroyed."""
        async with ctx.emit_lock:
            async with ctx.queued_query_lock:
                self._queued_query_count -= len(ctx.queued_queries)
                self._queued_query_bytes -= ctx.queued_query_bytes
                ctx.queued_queries = []
                ctx.queued_query_bytes = 0
                ctx.queued_query_errors = {}
                ctx.queued_query_starting_msg_id = None
                if publish:
                    await self._emit_locked(
                        ctx, self._query_queue_state(ctx))
        ctx.queued_query_wakeup.set()
        task = ctx.queued_query_drain_task
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ctx.queued_query_drain_task = None

    async def _set_state(self, ctx: SessionContext, state: State) -> None:
        ctx.state = state
        ctx.queued_query_wakeup.set()
        await self._emit(ctx, StateEvent(state=state))
        log.info("state transition", sid=ctx.session_id, state=state)

    async def _emit_current_state(
        self, ctx: SessionContext,
    ) -> StateEvent:
        """Publish a fresh authoritative state without changing lifecycle."""
        async with ctx.emit_lock:
            event = StateEvent(state=ctx.state)
            await self._emit_locked(ctx, event)
            return event

    # ---- command dispatch ----

    def _command_seen(self, client_id: str, cmd_id: str) -> tuple[bool, tuple[object, ...]]:
        bucket = self._processed_commands.get(client_id)
        if bucket is None or cmd_id not in bucket:
            return False, ()
        bucket.move_to_end(cmd_id)
        self._processed_commands.move_to_end(client_id)
        return True, bucket[cmd_id]

    def _remember_command(self, client_id: str, cmd_id: str,
                          responses: tuple[object, ...] = ()) -> None:
        response_bytes = self._command_response_bytes(responses)
        if response_bytes > self.COMMAND_RESPONSE_BYTES:
            # The command identity still suppresses a duplicate mutation. Its
            # oversized narrative remains available from the session ring or
            # canonical history instead of monopolizing the global retry cache.
            responses = ()
            response_bytes = 0
        bucket = self._processed_commands.get(client_id)
        if bucket is None:
            bucket = OrderedDict()
            self._processed_commands[client_id] = bucket
        previous_size = self._processed_command_sizes.pop(
            (client_id, cmd_id), 0)
        self._processed_command_bytes -= previous_size
        bucket[cmd_id] = responses
        self._processed_command_sizes[(client_id, cmd_id)] = response_bytes
        self._processed_command_bytes += response_bytes
        bucket.move_to_end(cmd_id)
        self._processed_commands.move_to_end(client_id)
        while len(bucket) > self.COMMAND_IDS_PER_CLIENT:
            dropped_id, _ = bucket.popitem(last=False)
            self._processed_command_bytes -= self._processed_command_sizes.pop(
                (client_id, dropped_id), 0)
        while len(self._processed_commands) > self.COMMAND_CLIENTS:
            dropped_client, dropped_bucket = (
                self._processed_commands.popitem(last=False))
            for dropped_id in dropped_bucket:
                self._processed_command_bytes -= (
                    self._processed_command_sizes.pop(
                        (dropped_client, dropped_id), 0))
        while (self._processed_command_bytes > self.COMMAND_RESPONSE_BYTES
               and self._processed_commands):
            dropped = False
            for oldest_client, oldest_bucket in self._processed_commands.items():
                for oldest_id in oldest_bucket:
                    key = (oldest_client, oldest_id)
                    size = self._processed_command_sizes.get(key, 0)
                    if size <= 0:
                        continue
                    # Preserve the at-most-once identity as an empty tombstone;
                    # only the replay payload is evicted under byte pressure.
                    oldest_bucket[oldest_id] = ()
                    self._processed_command_sizes[key] = 0
                    self._processed_command_bytes -= size
                    dropped = True
                    break
                if dropped:
                    break
            if not dropped:
                break

    @staticmethod
    def _command_response_bytes(responses: tuple[object, ...]) -> int:
        total = 0
        for response in responses:
            try:
                total += len(response.model_dump_json().encode("utf-8"))
            except Exception:
                total += 1024
        return total

    def _rekey_cached_create_responses(self, old_key: str, session_id: str,
                                       cwd: str) -> None:
        """Keep a cached create response usable if id capture happened while its
        original Focus/ACK were lost. Replay re-key first, then focus the real id."""
        for client_id, bucket in self._processed_commands.items():
            for cmd_id, responses in list(bucket.items()):
                updated: list[object] = []
                changed = False
                for response in responses:
                    if (isinstance(response, SessionFocus)
                            and response.session_id == old_key):
                        updated.append(SessionRekey(
                            old_key=old_key,
                            session_id=session_id,
                            cwd=cwd,
                            sid=session_id,
                            to=response.to,
                        ))
                        updated.append(response.model_copy(deep=True, update={
                            "session_id": session_id,
                            "cwd": cwd,
                            "sid": session_id,
                        }))
                        changed = True
                    elif (not isinstance(response, Snapshot)
                          and getattr(response, "sid", None) == old_key):
                        # Snapshot deliberately stays temp-keyed so the following
                        # SessionRekey has a runtime to migrate. All later cached
                        # frames must target the real id after that migration.
                        updated.append(response.model_copy(
                            deep=True, update={"sid": session_id}))
                        changed = True
                    else:
                        updated.append(response)
                if changed:
                    bucket[cmd_id] = tuple(updated)
                    key = (client_id, cmd_id)
                    previous_size = self._processed_command_sizes.get(key, 0)
                    next_size = self._command_response_bytes(bucket[cmd_id])
                    self._processed_command_sizes[key] = next_size
                    self._processed_command_bytes += next_size - previous_size

    async def _send_command_ack(self, client_id: str, cmd_id: str) -> None:
        await self.transport.send(CommandAck(
            cmd_id=cmd_id,
            client_id=client_id,
            to=client_id,
        ))

    async def _process_command_safely(self, cmd) -> None:
        """Run one command without letting a handler failure stop the loop."""
        try:
            await self._process_command(cmd)
        except Exception:
            log.exception("command handling failed", type=cmd.type)

    def _start_models_command(self, cmd) -> None:
        """Resolve catalogs/defaults without blocking the serial command lane.

        Keep the normal reliable-command lifecycle inside the task: Models is
        queued before CommandAck, and a reconnect retry with the same command id
        coalesces onto the still-running task instead of duplicating the read.
        """
        client_id = getattr(cmd, "client_id", None) or ""
        cmd_id = getattr(cmd, "cmd_id", None) or f"untracked-{id(cmd)}"
        key = (client_id, cmd_id)
        current = self._models_command_tasks.get(key)
        if current is not None and not current.done():
            log.debug("models command already resolving",
                      client_id=client_id, cmd_id=cmd_id)
            return

        task = asyncio.create_task(self._process_command_safely(cmd))
        self._models_command_tasks[key] = task

        def forget(done: asyncio.Task) -> None:
            if self._models_command_tasks.get(key) is done:
                self._models_command_tasks.pop(key, None)

        task.add_done_callback(forget)

        def forget(done: asyncio.Task) -> None:
            if self._status_command_tasks.get(key) is done:
                self._status_command_tasks.pop(key, None)

        task.add_done_callback(forget)

    def _start_session_list_command(self, cmd) -> None:
        """Resolve sidebar catalogs without blocking query/new-session input."""
        task = asyncio.create_task(self._process_command_safely(cmd))
        self._session_list_command_tasks.add(task)
        task.add_done_callback(self._session_list_command_tasks.discard)

    def _start_history_command(self, cmd) -> None:
        """Read one history page without blocking query/interrupt intake."""
        client_id = getattr(cmd, "client_id", None) or ""
        cmd_id = getattr(cmd, "cmd_id", None) or f"untracked-{id(cmd)}"
        key = (client_id, cmd_id)
        current = self._history_command_tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._process_command_safely(cmd))
        self._history_command_tasks[key] = task

        def forget(done: asyncio.Task) -> None:
            if self._history_command_tasks.get(key) is done:
                self._history_command_tasks.pop(key, None)

        task.add_done_callback(forget)

    def _start_capabilities_command(self, cmd) -> None:
        """Keep extension discovery off the serial query/mutation lane."""
        client_id = getattr(cmd, "client_id", None) or ""
        cmd_id = getattr(cmd, "cmd_id", None) or f"untracked-{id(cmd)}"
        key = (client_id, cmd_id)
        current = self._capabilities_command_tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._process_command_safely(cmd))
        self._capabilities_command_tasks[key] = task

        def forget(done: asyncio.Task) -> None:
            if self._capabilities_command_tasks.get(key) is done:
                self._capabilities_command_tasks.pop(key, None)

        task.add_done_callback(forget)

    def _start_interactive_control_command(self, cmd) -> None:
        """Run one answerable control without blocking the receive loop.

        Reliable command ids coalesce reconnect retries while the question is
        open.  The normal command processor still owns the final ACK/cache
        boundary after the user answers and the native TUI confirms the change.
        """
        client_id = getattr(cmd, "client_id", None) or ""
        cmd_id = getattr(cmd, "cmd_id", None) or f"untracked-{id(cmd)}"
        key = (client_id, cmd_id)
        current = self._interactive_control_tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._process_command_safely(cmd))
        self._interactive_control_tasks[key] = task

        def forget(done: asyncio.Task) -> None:
            if self._interactive_control_tasks.get(key) is done:
                self._interactive_control_tasks.pop(key, None)

        task.add_done_callback(forget)

    def _refresh_cached_response(self, response):
        """Copy a cached one-shot response, refreshing volatile snapshots.

        A create command can be retried after its first response was lost while
        the newly-created turn is already running. Replaying the original idle
        Snapshot would roll the client back to idle, so derive current state from
        the resident context without re-executing the command.
        """
        replay = response.model_copy(deep=True)
        if isinstance(replay, Snapshot) and replay.sid:
            ctx = self._ctx_for(replay.sid)
            if ctx is not None:
                replay.state = ctx.buffer.latest_state() or ctx.state
                replay.tail_text = ctx.buffer.latest_tail_text()
                replay.cc_session_id = self._ctx_wire_sid(ctx)
                replay.cwd = ctx.cwd
                replay.generation = self.instance_id
        return replay

    async def _process_command(self, cmd) -> None:
        """Deduplicate reliable client commands and ACK completed handlers.

        A business rejection that emits Error still returns normally and is
        acknowledged. An unexpected exception is deliberately neither remembered
        nor ACKed, so the client can retry instead of accepting partial failure.
        """
        client_id = getattr(cmd, "client_id", None)
        cmd_id = getattr(cmd, "cmd_id", None)
        reliable = is_reliable_command(cmd) and client_id and cmd_id
        seen, cached_responses = (
            self._command_seen(client_id, cmd_id) if reliable else (False, ()))
        if seen:
            if cmd.type in {"fork_session", "fork_session_worktree"}:
                entry = await self._fork_entry_for_cached_command(
                    cmd, cached_responses)
                if entry and entry.get("status") in {
                    "delete_pending", "deleted",
                }:
                    # Cached command responses predate the durable child
                    # tombstone. ACK the reliable retry but do not replay its
                    # now-deleted SessionForked navigation frame.
                    await self._send_command_ack(client_id, cmd_id)
                    return
            if cmd.type in self.SAFE_RETRY_COMMANDS:
                # The original one-shot response may have died on the same link as
                # its ACK. Safe reads and idempotent reconciliations are re-run
                # to recreate an authoritative response.
                await self._handle(cmd)
                log.info("duplicate safe command replayed", client_id=client_id,
                         cmd_id=cmd_id, type=cmd.type)
            else:
                switch_ctx = None
                if cmd.type == "switch_session":
                    response_sid = next((
                        getattr(response, "sid", None)
                        for response in cached_responses
                        if isinstance(response, StateEvent)
                    ), None)
                    switch_ctx = self._ctx_for(
                        response_sid or getattr(cmd, "session_id", None))
                for response in cached_responses:
                    # State is volatile. Replaying its cached event_seq can be
                    # rejected as stale after this session transitions while a
                    # switch ACK is in flight. A resident target is reseeded
                    # once below with its current value and a fresh sequence.
                    if (
                        switch_ctx is not None
                        and isinstance(response, StateEvent)
                    ):
                        continue
                    replay = self._refresh_cached_response(response)
                    # A cached business error may originally have been broadcast
                    # with its turn. Retry it only to the command's origin.
                    if getattr(replay, "to", None) is None:
                        replay.to = client_id
                    await self.transport.send(replay)
                if switch_ctx is not None:
                    await self._emit_current_state(switch_ctx)
                log.info("duplicate command suppressed", client_id=client_id,
                         cmd_id=cmd_id, type=cmd.type,
                         responses=len(cached_responses))
            await self._send_command_ack(client_id, cmd_id)
            return

        result = await self._handle(cmd)

        if reliable:
            # Safe commands are recreated on retry, so retaining their response
            # payloads in the command-id LRU buys nothing and can multiply
            # memory by clients × commands.
            if cmd.type in self.SAFE_RETRY_COMMANDS:
                responses = ()
            elif cmd.type == "steer":
                candidates = (
                    result if isinstance(result, (tuple, list)) else (result,))
                # Successful narrative is already in the bounded turn ring and
                # canonical history (with clientMsgId). Cache only correlated
                # failures; copying a legal multi-megabyte steer into the global
                # command LRU would duplicate user payloads.
                responses = tuple(
                    response.model_copy(deep=True)
                    for response in candidates
                    if isinstance(response, Error)
                )
            else:
                candidates = (
                    result if isinstance(result, (tuple, list)) else (result,))
                responses = tuple(
                    response.model_copy(deep=True)
                    for response in candidates
                    if getattr(response, "type", None)
                )
            self._remember_command(client_id, cmd_id, responses)
            await self._send_command_ack(client_id, cmd_id)

    async def _handle_client_hello(self, cmd) -> None:
        """A browser client (re)connected after the relay bound it.

        The relay forwards the client hello (with per-session cursors) to the
        wrapper. Replay each cursored resident session's ring delta so the
        client recovers its live tail, and publish a Snapshot for the focused
        session so the UI leaves the offline state (the reducer sets
        wrapperOnline on snapshot / replay_end frames).
        """
        await self.transport.send(self._engine_catalog(to=getattr(cmd, "client_id", None)))
        cursors = getattr(cmd, "cursors", None)
        seen: set[str] = set()
        if isinstance(cursors, dict):
            for sid, last_seq in cursors.items():
                if not isinstance(sid, str) or not isinstance(last_seq, int):
                    continue
                ctx = self.sessions.get(sid) or self._ctx_by_sid(sid)
                if ctx is None or ctx.key in seen:
                    continue
                seen.add(ctx.key)
                try:
                    frames = ctx.buffer.replay_from(
                        last_seq,
                        cc_session_id=ctx.session_id,
                        state=ctx.state,
                        tail_text=ctx.buffer.latest_tail_text(),
                        cwd=ctx.cwd,
                        generation=self.instance_id,
                    )
                except Exception as exc:
                    log.warning(
                        "client hello ring replay failed",
                        session_id=ctx.key,
                        error_type=type(exc).__name__,
                    )
                    continue
                for frame in frames:
                    frame.sid = ctx.key
                    await self.transport.send(frame)
        # Queue state is a current projection, never a historical ring delta.
        # Send it last so replaying an old enqueue cannot resurrect sent items.
        for resident in tuple(self.sessions.values()):
            queue = self._query_queue_state(resident)
            queue.sid = self._ctx_wire_sid(resident)
            queue.to = getattr(cmd, "client_id", None)
            await self.transport.send(queue)
        # Mark the wrapper online even when no cursor matched: the reducer
        # sets wrapperOnline on snapshot / replay_end, and the composer leaves
        # its offline state only once that happens. Prefer the focused session,
        # then any resident session; a brand-new wrapper with an empty pool
        # self-heals on the client's first switch (which spawns + snapshots).
        target = self.sessions.get(self.focused_sid or "")
        if target is None or target.key in seen:
            target = next((
                ctx for ctx in self.sessions.values()
                if ctx.key not in seen
            ), None)
        if target is not None:
            snap = Snapshot(
                cc_session_id=target.key,
                state=target.state,
                tail_text=target.buffer.latest_tail_text(),
                cwd=target.cwd,
                generation=self.instance_id,
                control=self._session_control(target),
            )
            snap.sid = target.key
            await self.transport.send(snap)

    async def _handle(self, cmd) -> None:
        adapter = getattr(self, "_codex", None)
        if adapter is not None:
            result = await adapter.dispatch(cmd)
            if result is not UNHANDLED_COMMAND:
                return result
        result = await self._command_router.dispatch(cmd)
        if result is UNHANDLED_COMMAND:
            log.warning(
                "unexpected command",
                type=cmd.type,
                role=getattr(cmd, "role", None),
            )
            await self._reject_unhandled_command(cmd)
            return None
        return result

    async def _reject_unhandled_command(self, cmd) -> None:
        """Answer a command this wrapper cannot serve.

        Silence is the worst possible answer: the browser correlates the frame
        it is waiting for by ``request_id``/``cmd_id``, so a dropped command
        leaves its spinner running forever without a word. Every unmapped client
        command therefore gets a correlated, human-readable rejection.
        """
        client_id = getattr(cmd, "client_id", None)
        if not client_id:
            # An ownerless command (no client bound by the relay) must fail
            # closed: a broadcast answer would leak the rejection to every
            # connected browser and could expose a private session id.
            return None
        request_id = (
            getattr(cmd, "request_id", None) or getattr(cmd, "cmd_id", None)
        )
        error = Error(
            code=ERR_PROTOCOL,
            message="当前远程后端不支持该操作，请更新页面后重试。",
            request_id=request_id,
            sid=getattr(cmd, "sid", None) or getattr(cmd, "session_id", None),
            to=client_id,
        )
        await self.transport.send(error)
        return error

    @staticmethod
    def _frame_reasserts_active_binding(
        frame, binding: ActiveTurnBinding,
    ) -> bool:
        return isinstance(frame, (TurnBinding, TurnSteered)) and (
            frame.msg_id == binding.msg_id
            and frame.turn_id == binding.turn_id
        )

    def _reseed_active_binding_for_hello(
        self,
        ctx: SessionContext,
        frames: list,
        *,
        cursor: Optional[int],
        same_generation: bool,
    ) -> list:
        """Place one client-only owner seed at a proven replay boundary.

        The seed is deliberately unsequenced and never enters the shared ring:
        it restores reducer ownership which a page reload forgot without moving
        that client's durable cursor.  If the replay contains frames from an
        unknown prefix (truncated/rebuild), seed only after ReplayEnd so those
        old frames cannot be attached to the newest steer segment.
        """
        binding = ctx.active_turn_binding
        if (
            binding is None
            or ctx.state == "idle"
            or binding.generation != self.instance_id
            or any(self._frame_reasserts_active_binding(frame, binding)
                   for frame in frames)
        ):
            return frames

        seed = TurnBinding(
            msg_id=binding.msg_id,
            turn_id=binding.turn_id,
        )
        replay_start_index = next((
            index for index, frame in enumerate(frames)
            if isinstance(frame, ReplayStart)
        ), None)
        replay_end_index = next((
            index for index in range(len(frames) - 1, -1, -1)
            if isinstance(frames[index], ReplayEnd)
        ), None)

        if replay_start_index is None:
            snapshot_index = next((
                index for index, frame in enumerate(frames)
                if isinstance(frame, Snapshot)
            ), None)
            insert_at = (snapshot_index + 1
                         if snapshot_index is not None else 0)
        else:
            replay_start = frames[replay_start_index]
            ambiguous_prefix = (
                replay_start.truncated
                or replay_start.rebuild
                or not same_generation
            )
            binding_already_consumed = (
                same_generation
                and cursor is not None
                and cursor >= binding.seq
            )
            # Even a truncated/rebuild envelope has an exact current-turn
            # suffix when its first retained sequence is strictly newer than
            # the resident binding.  The binding may have fallen out of the
            # bounded ring, but monotonic per-session ordering proves that no
            # replayed narrative frame can precede this logical owner.
            retained_suffix_after_binding = (
                replay_start.from_seq > binding.seq
            )
            if binding_already_consumed or retained_suffix_after_binding or (
                not ambiguous_prefix and cursor is None
            ):
                insert_at = replay_start_index + 1
            else:
                # A non-truncated cursor below binding.seq should have replayed
                # the original binding. If it did not, fail closed just like a
                # truncated prefix: do not claim earlier narrative as current.
                insert_at = (replay_end_index + 1
                             if replay_end_index is not None else len(frames))
        return [*frames[:insert_at], seed, *frames[insert_at:]]

    def _history_refresh_in_progress(self, sid: str) -> bool:
        ctx = self._ctx_by_sid(sid)
        active_external_turns = (
            (self._watch.get(sid) or {}).get("active_external_turns")
        )
        return bool(
            (ctx is not None and ctx.state != "idle")
            or (
                isinstance(active_external_turns, dict)
                and active_external_turns
            )
        )

    def _history_refresh_backoff_seconds(self, scan_elapsed: float) -> float:
        return min(
            max(self.HISTORY_REFRESH_MIN_INTERVAL_SECONDS, scan_elapsed),
            self.HISTORY_REFRESH_MAX_INTERVAL_SECONDS,
        )

    async def _build_dsh_history(
        self,
        sid: str,
        *,
        before: str | None,
        limit: int | None,
        detail: str,
        command_aliases: dict[str, str] | None = None,
    ) -> History:
        revision = self._history_revision(sid)
        if before is None:
            build_seq = self._history_build_sequences.get(sid, 0) + 1
            self._history_build_sequences[sid] = build_seq
        else:
            build_seq = self._history_build_sequences.get(sid, 0)
        ctx = self._ctx_by_sid(sid)
        try:
            native_sid = dsh_native_session_id(sid)
            _client, history = self._dsh_client_ready()
            aliases: dict[str, str] = {}
            if ctx is not None and ctx.engine == "dsh":
                aliases.update(ctx.dsh_command_aliases)
            if command_aliases:
                aliases.update(command_aliases)
            # The newest page needs a throughSeq bound (DSH 0.1.2). Prefer the
            # live follow watermark; fall back to a transient cursor fetch.
            through_seq = None
            if ctx is not None and ctx.engine == "dsh":
                through_seq = ctx.dsh_last_source_seq
            if through_seq is None and ctx is not None and ctx.engine == "dsh":
                try:
                    through_seq = await _client.fetch_session_cursor(native_sid)
                except Exception:
                    through_seq = None
            page = await history.page(
                native_sid,
                wire_session_id=sid,
                before=before,
                limit=limit or 4,
                command_aliases=aliases or None,
                through_seq=through_seq,
            )
            if (
                before is None
                and ctx is not None
                and ctx.engine == "dsh"
            ):
                async with ctx.dsh_event_lock:
                    if isinstance(ctx.sdk, DshSessionHandle):
                        ctx.sdk.apply_projections(
                            page.projections, page.projection_seq,
                        )
                    if page.last_seq >= ctx.dsh_last_source_seq:
                        ctx.dsh_last_source_seq = page.last_seq
                        ctx.dsh_translator = page.translator
            title = page.projections.get("title")
            if isinstance(title, str) and title:
                self._remember_notification_title(sid, title)
            return History(
                session_id=sid,
                revision=revision,
                generation=self.instance_id,
                build_seq=build_seq,
                live_seq=ctx.seq if ctx is not None else None,
                events=(list(page.events) if detail == "full" else []),
                turns=(list(page.turns) if detail == "summary" else []),
                detail=detail,
                has_more=page.has_more,
                before=before,
                control=(
                    self._session_control(ctx)
                    if ctx is not None and ctx.engine == "dsh" else None
                ),
                oldest_id=page.oldest_id,
                newest_id=page.newest_id,
                external=False,
                in_progress=bool(
                    ctx is not None and ctx.engine == "dsh"
                    and ctx.state != "idle"
                ),
            )
        except Exception as exc:
            log.warning(
                "DSH history failed",
                session_id=sid,
                error_type=type(exc).__name__,
            )
            return History(
                session_id=sid,
                revision=revision,
                generation=self.instance_id,
                build_seq=build_seq,
                live_seq=ctx.seq if ctx is not None else None,
                authoritative=False,
                error=_dsh_history_error_message(exc),
                events=[],
                turns=[],
                detail=detail,
                has_more=False,
                before=before,
                control=(
                    self._session_control(ctx)
                    if ctx is not None and ctx.engine == "dsh" else None
                ),
                external=False,
                in_progress=bool(
                    ctx is not None and ctx.engine == "dsh"
                    and ctx.state != "idle"
                ),
            )

    async def _push_dsh_history(self, ctx: SessionContext) -> History | None:
        sid = self._ctx_wire_sid(ctx)
        if ctx.engine != "dsh" or not sid:
            return None
        history = await self._build_dsh_history(
            sid, before=None, limit=4, detail="summary"
        )
        history.sid = sid
        await self.transport.send(history)
        return history

    async def _build_requested_history(
        self,
        sid: str,
        *,
        before: str | None,
        limit: int | None,
        cwd: str | None,
        detail: str,
    ) -> History:
        """Build one requester-neutral page shared by concurrent clients."""
        if sid.startswith("dsh@"):
            return await self._build_dsh_history(
                sid,
                before=before,
                limit=limit,
                detail=detail,
            )
        # DSH-only wrapper: a resident session under its temporary pre-capture
        # key has no durable history yet; answer an empty page.
        ctx = self._ctx_by_sid(sid)
        return History(
            session_id=sid,
            revision=self._history_revision(sid),
            generation=self.instance_id,
            build_seq=self._history_build_sequences.get(sid, 0),
            live_seq=ctx.seq if ctx is not None else None,
            authoritative=False,
            error=None,
            events=[],
            turns=[],
            detail=detail,
            has_more=False,
            before=before,
            external=False,
            in_progress=bool(ctx is not None and ctx.state != "idle"),
        )

    async def _history_page_singleflight(self, cmd, sid: str) -> History:
        before = getattr(cmd, "before", None)
        raw_limit = getattr(cmd, "limit", None)
        limit = int(raw_limit) if isinstance(raw_limit, int) else None
        cwd = getattr(cmd, "cwd", None)
        detail = getattr(cmd, "detail", "full")
        key = (sid, before or "", limit or 0, f"{cwd or ''}\0{detail}")
        task = self._history_page_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._build_requested_history(
                sid, before=before, limit=limit, cwd=cwd, detail=detail))
            self._history_page_tasks[key] = task

            def forget(done: asyncio.Task) -> None:
                if self._history_page_tasks.get(key) is done:
                    self._history_page_tasks.pop(key, None)

            task.add_done_callback(forget)
        # A browser disconnect cancels only its routing handler; other clients
        # awaiting the same immutable page keep the shared read alive.
        return await asyncio.shield(task)

    async def _handle_get_turn_detail(self, cmd) -> TurnDetail:
        """Return one heavyweight turn projection to its requesting browser."""
        started_at = time.perf_counter()
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        revision = self._history_revision(sid)
        client_id = getattr(cmd, "client_id", None)

        async def send(
            events: list[dict] | None = None,
            *,
            error: str | None = None,
            has_more: bool = False,
            oldest_cursor: str | None = None,
            has_newer: bool = False,
            newer_cursor: str | None = None,
        ) -> TurnDetail:
            detail = TurnDetail(
                session_id=sid,
                turn_id=cmd.turn_id,
                revision=revision,
                authoritative=error is None,
                error=error,
                events=events or [],
                has_more=has_more,
                oldest_cursor=oldest_cursor,
                has_newer=has_newer,
                newer_cursor=newer_cursor,
                before=getattr(cmd, "before", None),
                sid=sid,
                to=client_id,
            )
            await self.transport.send(detail)
            frame_bytes = len(detail.model_dump_json().encode("utf-8"))
            log.info(
                "turn detail sent",
                session_id=sid,
                turn_id=cmd.turn_id,
                events=len(detail.events),
                frame_bytes=frame_bytes,
                authoritative=detail.authoritative,
                has_more=detail.has_more,
                has_newer=detail.has_newer,
                client_id=client_id,
                elapsed_ms=round((time.perf_counter() - started_at) * 1000),
            )
            return detail

        requested_revision = getattr(cmd, "revision", None)
        if requested_revision and requested_revision != revision:
            return await send(error="会话历史已更新，请重新展开该轮")

        if sid.startswith("dsh@"):
            history = self._dsh_history
            rows = history.detail(sid, cmd.turn_id) if history else None
            if rows is None:
                return await send(
                    error="详细过程已过期，请刷新会话后重新展开"
                )
            try:
                page, has_more, oldest, has_newer, newer = (
                    _turn_detail_page(
                        rows,
                        before=getattr(cmd, "before", None),
                        limit=getattr(cmd, "limit", 192),
                        max_bytes=min(
                            8 * 1024 * 1024,
                            max(
                                512 * 1024,
                                self.cfg.ws_max_size_bytes // 2,
                            ),
                        ),
                    )
                )
            except ValueError:
                return await send(
                    error="详细过程分页位置已失效，请重新展开该轮"
                )
            return await send(
                page,
                has_more=has_more,
                oldest_cursor=oldest,
                has_newer=has_newer,
                newer_cursor=newer,
            )

        # DSH-only wrapper: every resident id is a dsh@ wire id served above.
        return await send(error="详细过程暂时不可用，请稍后重试")

    # ---- artifact previews: the file viewer and the git diff panel ----------
    #
    # Both commands are answered from the wrapper host, bounded in bytes, and
    # confined to the session working directory. The DSH-only rewrite dropped
    # them wholesale, which left the client's request unsettled: tapping a file
    # the agent had changed spun forever because no frame ever came back.

    PREVIEW_IMAGE_MEDIA_TYPES = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
    }
    PREVIEW_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdx"})
    PREVIEW_BINARY_MAX_BYTES = 8 * 1024 * 1024

    @classmethod
    def _preview_format(cls, path: str) -> str:
        suffix = os.path.splitext(path)[1].lower()
        if suffix in cls.PREVIEW_MARKDOWN_SUFFIXES:
            return "markdown"
        if suffix == ".pdf":
            return "pdf"
        if suffix in cls.PREVIEW_IMAGE_MEDIA_TYPES:
            return "image"
        return "text"

    def _preview_resolve(self, cwd: str, path: str) -> tuple[str, str]:
        """Resolve one requested path, refusing anything outside the session."""
        root = os.path.realpath(cwd)
        expanded = os.path.expanduser(path)
        candidate = os.path.abspath(
            expanded if os.path.isabs(expanded) else os.path.join(root, expanded)
        )
        resolved = os.path.realpath(candidate)
        if not self._path_is_below(root, resolved):
            raise ValueError("只能预览本会话工作目录内的文件")
        return root, resolved

    @classmethod
    def _read_file_preview(
        cls, resolved: str, display_path: str,
    ) -> dict[str, object]:
        """Read one artifact on the wrapper host; nothing is persisted here."""
        file_stat = os.stat(resolved)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("只能预览普通文件")
        suffix = os.path.splitext(resolved)[1].lower()
        media_type = cls.PREVIEW_IMAGE_MEDIA_TYPES.get(suffix)
        if media_type is not None:
            limit = cls.PREVIEW_BINARY_MAX_BYTES
            with open(resolved, "rb") as handle:
                data = handle.read(limit + 1)
            if len(data) > limit:
                raise ValueError("文件过大，暂不支持预览")
            return {
                "path": display_path,
                "format": "pdf" if media_type == "application/pdf" else "image",
                "media_type": media_type,
                "data": data,
                "size": file_stat.st_size,
                "mtime_ns": file_stat.st_mtime_ns,
                "revision": hashlib.sha256(data).hexdigest(),
            }

        limit = FILE_PREVIEW_MAX_BYTES
        with open(resolved, "rb") as handle:
            raw = handle.read(limit + 1)
        truncated = len(raw) > limit
        payload = raw[:limit]
        if b"\0" in payload:
            raise ValueError("二进制文件暂不支持预览")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError:
            if not truncated:
                raise ValueError("文件不是有效的 UTF-8 文本") from None
            # A byte cut can land inside one character; drop the partial tail.
            content = payload.decode("utf-8", errors="ignore")
        return {
            "path": display_path,
            "format": cls._preview_format(resolved),
            "content": content,
            "size": file_stat.st_size,
            "truncated": truncated,
            "mtime_ns": file_stat.st_mtime_ns,
            "revision": None if truncated else hashlib.sha256(payload).hexdigest(),
        }

    def _browse_workspace_files(self, cwd: str, path: str, query: str) -> dict:
        root, directory = self._preview_resolve(cwd, path or ".")
        if not os.path.isdir(directory):
            raise ValueError("请选择目录")
        entries = []
        scanned = 0
        truncated = False
        deadline = time.monotonic() + 1.5
        skip = {"node_modules", "__pycache__", "venv", "dist", "build"}
        pending = [directory]
        while pending:
            current = pending.pop()
            with os.scandir(current) as listing:
                for entry in listing:
                    scanned += 1
                    if scanned > 10000 or time.monotonic() > deadline or len(entries) >= 200:
                        truncated = True
                        break
                    if entry.name.startswith(".") or entry.is_symlink():
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if not is_dir and not entry.is_file(follow_symlinks=False):
                        continue
                    relative = os.path.relpath(entry.path, root)
                    if query and is_dir and entry.name not in skip:
                        pending.append(entry.path)
                    if not query or (not is_dir and query.casefold() in relative.casefold()):
                        entries.append(FileEntry(name=entry.name, path=relative,
                                                 kind="directory" if is_dir else "file"))
            if truncated:
                break
        entries.sort(key=lambda entry: (entry.kind != "directory", entry.path.casefold()))
        return {"path": os.path.relpath(directory, root),
                "parent": None if directory == root else os.path.relpath(os.path.dirname(directory), root),
                "entries": entries, "truncated": truncated}

    async def _handle_browse_files(self, cmd) -> FilesList:
        sid = getattr(cmd, "sid", None)
        ctx = self._ctx_by_sid(sid) if sid else None
        response = FilesList(path=cmd.path, request_id=cmd.request_id,
                             sid=sid, to=getattr(cmd, "client_id", None))
        try:
            if ctx is None or not ctx.cwd:
                raise ValueError("该会话未启动，无法读取目录")
            listing = await asyncio.to_thread(self._browse_workspace_files,
                                             ctx.cwd, cmd.path, cmd.query.strip())
            response = response.model_copy(update=listing)
        except (OSError, ValueError) as exc:
            response.error = str(exc) or "读取目录失败"
        except Exception:
            log.exception("file browser failed")
            response.error = "读取目录失败"
        await self.transport.send(response)
        return response

    async def _handle_get_file_preview(self, cmd) -> FilePreview:
        sid = getattr(cmd, "sid", None) or getattr(cmd, "session_id", None)
        client_id = getattr(cmd, "client_id", None)
        ctx = self._ctx_by_sid(sid) if sid else self._focused_ctx()

        def failure(message: str) -> FilePreview:
            return FilePreview(
                path=cmd.path,
                request_id=cmd.request_id,
                format=self._preview_format(cmd.path),
                error=message or "读取文件失败",
                to=client_id,
                sid=sid,
            )

        if ctx is None or not ctx.cwd:
            response = failure("该会话未启动，无法读取文件")
            await self.transport.send(response)
            return response
        try:
            root, resolved = self._preview_resolve(ctx.cwd, cmd.path)
            display_path = (
                cmd.path if os.path.isabs(cmd.path)
                else os.path.relpath(resolved, root)
            )
            preview = await asyncio.to_thread(
                self._read_file_preview, resolved, display_path)
            raw_data = preview.get("data")
            response = FilePreview(
                path=str(preview["path"]),
                request_id=cmd.request_id,
                format=str(preview.get("format", "text")),
                content=str(preview.get("content", "")),
                media_type=preview.get("media_type"),
                data=(
                    base64.b64encode(raw_data).decode("ascii")
                    if isinstance(raw_data, bytes) else None
                ),
                size=int(preview.get("size", 0)),
                truncated=bool(preview.get("truncated", False)),
                mtime_ns=str(preview.get("mtime_ns", 0)),
                revision=preview.get("revision"),
                writable=True,
                to=client_id,
                sid=sid,
            )
        except (OSError, ValueError) as exc:
            response = failure(str(exc))
        except Exception as exc:  # noqa: BLE001 - never leave the client waiting
            log.exception("file preview failed", error_type=type(exc).__name__)
            response = failure("读取文件失败")
        await self.transport.send(response)
        log.info(
            "file preview sent",
            session_id=sid,
            path=response.path,
            format=response.format,
            size=response.size,
            truncated=response.truncated,
            error=response.error,
            client_id=client_id,
        )
        return response

    async def _handle_get_diff(self, cmd) -> None:
        """Return the session repository's diff for one file, or for all of it."""
        sid = getattr(cmd, "sid", None) or getattr(cmd, "session_id", None)
        client_id = getattr(cmd, "client_id", None)
        request_id = getattr(cmd, "cmd_id", None)
        ctx = self._ctx_by_sid(sid) if sid else None
        ctx = ctx or self._focused_ctx()

        async def send(payload) -> None:
            payload.to = client_id
            await self.transport.send(payload)

        if ctx is None or not ctx.cwd:
            await send(Error(
                code=ERR_NOT_RUNNING,
                message="该会话未启动，无法读取代码差异",
                request_id=request_id,
                sid=sid,
            ))
            return
        max_bytes = max(
            64 * 1024,
            min(4 * 1024 * 1024, self.cfg.ws_max_size_bytes // 2),
        )
        try:
            diff = await read_git_diff(
                ctx.cwd,
                cmd.file,
                max_bytes=max_bytes,
                source_max_bytes=self.cfg.history_source_max_bytes,
                run_command=bounded_process_output,
            )
        except Exception as exc:  # noqa: BLE001 - answer instead of hanging
            log.warning(
                "git diff failed",
                session_id=sid,
                file=cmd.file,
                error_type=type(exc).__name__,
            )
            await send(Error(
                code=ERR_INTERNAL,
                message="文件差异暂不可用，请稍后重试。",
                request_id=request_id,
                sid=sid,
            ))
            return
        report = DiffReport(file=cmd.file, diff=diff, request_id=request_id, sid=sid)
        await send(report)
        log.info(
            "git diff sent",
            session_id=sid,
            file=cmd.file,
            bytes=len(diff),
            client_id=client_id,
        )

    async def _handle_get_history(self, cmd) -> None:
        """Client opened a session: return its history as ONE bulk frame, routed to
        the requester. Opening it also registers the resident watch so live DSH
        stream appends mirror to every client (read-only)."""
        started_at = time.perf_counter()
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        template = await self._history_page_singleflight(cmd, sid)
        # Routing and command correlation are requester-specific; never mutate
        # the shared task's History instance in place.
        hist = template.model_copy(deep=True)
        hist.sid = sid
        await self.transport.send(hist)
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        if hist.authoritative is False and hist.error is not None:
            log.warning(
                "history provisional failure",
                session_id=sid,
                elapsed_ms=elapsed_ms,
            )
        else:
            log.info(
                "history served",
                session_id=sid,
                turns=len(hist.turns),
                elapsed_ms=elapsed_ms,
            )
        if sid.startswith("dsh@"):
            self._watch_session(sid)
            # A reloaded page must see prompts this session is still blocked on.
            await self._resend_pending_asks(sid, getattr(cmd, "client_id", None))
        self._remember_command(cmd.client_id, cmd.cmd_id, (hist,))

    async def _resend_pending_asks(self, sid: str, to: str | None) -> int:
        """Re-deliver prompts this session is still blocked on.

        A waterfall is delivered exactly once. If the browser reloads, or iOS
        discards the tab, the card disappears while the agent keeps waiting — and
        an answer the user tapped just before that died with the in-memory
        outbox, so the session looks stuck with nothing left to press (measured
        once in the field: a question raised at 09:25 whose answer never
        arrived, with the agent waiting until the ask timed out).

        Re-emitting on attach is safe: ``ask_id`` is the identity answers are
        correlated with, so a duplicate card is the same card.
        """
        ctx = self._ctx_by_sid(sid)
        if ctx is None or ctx.engine not in {"dsh", "codex"} or not ctx.pending_asks:
            return 0
        resent = 0
        for ask_id, future in list(ctx.pending_asks.items()):
            if future.done():
                continue
            spec = ctx.pending_ask_specs.get(ask_id)
            event = spec.get("event") if isinstance(spec, dict) else None
            if not isinstance(event, AskUser):
                continue
            replay = event.model_copy(deep=True)
            replay.to = to
            await self._emit(ctx, replay)
            resent += 1
        if resent:
            log.info(
                "ask_user re-sent on attach",
                session_id=sid,
                count=resent,
                to=to,
            )
        return resent

    def _watch_session(self, sid: str) -> None:
        """Register a session for live mirroring (no-op bookkeeping for DSH)."""
        self._watch.setdefault(sid, {"path": None, "size": 0, "engine": "dsh"})


    async def _handle_get_history_image(self, cmd) -> HistoryImage:
        """Return one source-bound historical image without resuming an engine."""
        started_at = time.perf_counter()
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        revision = self._history_revision(sid)
        client_id = getattr(cmd, "client_id", None)

        async def send(
            *,
            media_type: str | None = None,
            width: int | None = None,
            height: int | None = None,
            data: bytes | None = None,
            error: str | None = None,
        ) -> HistoryImage:
            response = HistoryImage(
                session_id=sid,
                turn_id=cmd.turn_id,
                image_id=cmd.image_id,
                variant=cmd.variant,
                request_id=cmd.request_id,
                revision=revision,
                media_type=media_type,
                width=width,
                height=height,
                data=(base64.b64encode(data).decode("ascii")
                      if data is not None else None),
                error=error,
                sid=sid,
                to=client_id,
            )
            await self.transport.send(response)
            log.info(
                "history image sent",
                session_id=sid,
                turn_id=cmd.turn_id,
                image_id=cmd.image_id,
                variant=cmd.variant,
                bytes=len(data or b""),
                authoritative=error is None,
                client_id=client_id,
                elapsed_ms=round((time.perf_counter() - started_at) * 1000),
            )
            return response

        requested_revision = getattr(cmd, "revision", None)
        if requested_revision and requested_revision != revision:
            return await send(error="会话历史已更新，请重新加载图片")

        if sid.startswith("dsh@"):
            try:
                native_sid = dsh_native_session_id(sid)
                _client, history = self._dsh_client_ready()
                source = await history.image(
                    native_sid,
                    cmd.turn_id,
                    cmd.image_id,
                    wire_session_id=sid,
                )
                if source is None:
                    return await send(error="历史图片索引已过期，请刷新会话")
                media_type, width, height, data = await asyncio.to_thread(
                    _render_history_image,
                    {
                        "media_type": source.media_type,
                        "data": source.data,
                    },
                    cmd.variant,
                )
                return await send(
                    media_type=media_type,
                    width=width,
                    height=height,
                    data=data,
                )
            except Exception as exc:
                log.warning(
                    "DSH history image failed",
                    session_id=sid,
                    image_id=cmd.image_id,
                    error_type=type(exc).__name__,
                )
                return await send(error="历史图片暂时不可用，请稍后重试")

        # DSH-only wrapper: every resident id is a dsh@ wire id served above.
        return await send(error="历史图片暂时不可用，请稍后重试")

    async def _refresh_dsh_command_history(
        self,
        ctx: SessionContext,
        command_id: str,
        msg_id: str,
    ) -> None:
        """Publish the native command row once its browser alias is visible."""
        sid = self._ctx_wire_sid(ctx)
        if ctx.engine != "dsh" or not sid:
            return
        for delay in self.DSH_RECEIPT_RECONCILE_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            if self._ctx_by_sid(sid) is not ctx:
                return
            history = await self._build_dsh_history(
                sid,
                before=None,
                limit=8,
                detail="summary",
                command_aliases={command_id: msg_id},
            )
            if not history.authoritative or not any(
                turn.clientMsgId == msg_id for turn in history.turns
            ):
                continue
            history.sid = sid
            await self.transport.send(history)
            return
        log.info(
            "DSH command history alias deferred",
            session_id=sid,
            msg_id=msg_id,
        )

    def _remember_dsh_command_alias(
        self,
        ctx: SessionContext,
        command_id: str,
        msg_id: str,
    ) -> None:
        """Bind one native command lifecycle to its browser presentation.

        ``dict`` preserves insertion order on every supported Python version.
        Reinsert an existing key to make this a small LRU without adding a
        second cache type to the already-large resident context.
        """
        ctx.dsh_command_aliases.pop(command_id, None)
        ctx.dsh_command_aliases[command_id] = msg_id
        while len(ctx.dsh_command_aliases) > self.DSH_COMMAND_ALIAS_CAP:
            oldest = next(iter(ctx.dsh_command_aliases))
            ctx.dsh_command_aliases.pop(oldest, None)

    async def _finish_dsh_command(
        self,
        ctx: SessionContext,
        *,
        msg_id: str,
        prompt: str,
        name: str,
        kind: str,
        text: str | None,
        command_id: str | None,
        interrupted: bool = False,
    ) -> None:
        """Close one standalone DSH command without inventing a fork point."""
        if ctx.dsh_pending_command_msg_id != msg_id:
            return
        if command_id is not None:
            self._remember_dsh_command_alias(ctx, command_id, msg_id)
        item_id = dsh_command_item_id(command_id or msg_id, msg_id)
        status = "interrupted" if interrupted else (
            "succeeded" if kind == "success" else "failed"
        )
        terminal = TurnEnd(
            presentation_id=msg_id,
            result=TurnResult(
                subtype=(
                    "error_during_execution" if interrupted
                    else "command_success" if kind == "success"
                    else "command_error"
                ),
                duration_ms=0,
                is_error=interrupted or kind == "error",
            ),
        )
        messages = (
            UserMsg(
                msg_id=msg_id,
                client_msg_id=msg_id,
                prompt=prompt,
            ),
            ProcessEvent(
                item_id=item_id,
                kind="task",
                phase="end",
                status=status,
                title=f"/{name}"[:1024],
                command=prompt,
                output=text[:2 * 1024 * 1024] if text else None,
                duration_ms=0,
            ),
            terminal,
        )
        for message in messages:
            try:
                await self._emit(ctx, message)
            except Exception as exc:
                # _emit appends to the per-session replay ring before trying
                # the live transport. Continue so every lifecycle boundary is
                # recoverable after this socket reconnects.
                log.debug(
                    "DSH command live projection delayed",
                    session_id=self._ctx_wire_sid(ctx),
                    message_type=message.type,
                    error_type=type(exc).__name__,
                )
        ctx.dsh_pending_command_msg_id = None
        ctx.dsh_pending_command_prompt = None
        ctx.active_msg_id = None
        ctx.interrupt_deadline = None
        ctx.interrupt_event.clear()
        if ctx.state != "idle":
            try:
                await self._set_state(ctx, "idle")
            except Exception as exc:
                log.debug(
                    "DSH command idle projection delayed",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )
        if command_id is not None:
            self._track_dsh_task(
                self._refresh_dsh_command_history(ctx, command_id, msg_id),
                name=f"cc-remote-dsh-command-history-{msg_id}",
            )
        try:
            await self._invalidate_session_list("dsh", "code")
        except Exception as exc:
            # The durable command and its per-session ring are already closed.
            # A sidebar refresh hint is best effort and must not prevent the
            # canonical aliased history page scheduled above.
            log.debug(
                "DSH command list invalidation delayed",
                session_id=self._ctx_wire_sid(ctx),
                error_type=type(exc).__name__,
            )

    async def _fail_dsh_command_unknown(
        self,
        ctx: SessionContext,
        *,
        msg_id: str,
        prompt: str,
        name: str,
    ) -> None:
        """Fail closed when commands/execute lost its native commandId."""
        if ctx.dsh_pending_command_msg_id != msg_id:
            return
        messages = (
            UserMsg(
                msg_id=msg_id,
                client_msg_id=msg_id,
                prompt=prompt,
            ),
            ProcessEvent(
                item_id=dsh_command_item_id(msg_id, msg_id),
                kind="task",
                phase="end",
                status="failed",
                title=f"/{name}"[:1024],
                command=prompt,
                summary="命令回执丢失，执行结果未知",
            ),
        )
        for message in messages:
            try:
                await self._emit(ctx, message)
            except Exception as exc:
                log.debug(
                    "DSH unknown command projection delayed",
                    session_id=self._ctx_wire_sid(ctx),
                    message_type=message.type,
                    error_type=type(exc).__name__,
                )
        ctx.dsh_pending_command_msg_id = None
        ctx.dsh_pending_command_prompt = None
        ctx.active_msg_id = None
        ctx.interrupt_deadline = None
        ctx.interrupt_event.clear()
        error = Error(
            code=ERR_INTERNAL,
            message=(
                "DeepSeek Harness 命令回执丢失，执行结果未知。"
                "请先刷新历史核对，确认前不要重复执行。"
            ),
            msg_id=msg_id,
        )
        try:
            await self._emit(ctx, error)
        except Exception as exc:
            log.debug(
                "DSH unknown command error delivery delayed",
                session_id=self._ctx_wire_sid(ctx),
                error_type=type(exc).__name__,
            )
        if ctx.state != "idle":
            try:
                await self._set_state(ctx, "idle")
            except Exception as exc:
                log.debug(
                    "DSH unknown command idle projection delayed",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )

    async def _run_dsh_command(
        self,
        ctx: SessionContext,
        prompt: str,
        msg_id: str,
        name: str,
    ) -> None:
        """Own a cancellable, potentially long-running DSH command request."""
        task = asyncio.current_task()
        try:
            if not isinstance(ctx.sdk, DshSessionHandle):
                raise DshProtocolError("DSH command handle is unavailable")
            execution = await ctx.sdk.execute_command(
                prompt, rpc_id=msg_id,
            )
        except asyncio.CancelledError:
            if ctx.turn_task is task:
                ctx.turn_task = None
            await self._finish_dsh_command(
                ctx,
                msg_id=msg_id,
                prompt=prompt,
                name=name,
                kind="error",
                text="命令已由 Remote 打断",
                command_id=None,
                interrupted=True,
            )
            return
        except Exception as exc:
            if ctx.turn_task is task:
                ctx.turn_task = None
            log.warning(
                "DSH command outcome unknown",
                session_id=self._ctx_wire_sid(ctx),
                error_type=type(exc).__name__,
            )
            await self._fail_dsh_command_unknown(
                ctx,
                msg_id=msg_id,
                prompt=prompt,
                name=name,
            )
            return
        if ctx.turn_task is task:
            ctx.turn_task = None
        await self._finish_dsh_command(
            ctx,
            msg_id=msg_id,
            prompt=prompt,
            name=name,
            kind=execution.kind,
            text=execution.text,
            command_id=execution.command_id,
        )

    async def _handle_query(self, cmd):
        sid = getattr(cmd, "sid", None)
        ctx = self._ctx_for(sid)
        if ctx is None:
            # sid given but not resident (spawn failed / evicted). Tag the error to
            # THAT session so the user sees it there — and never reroute the prompt
            # to a different session.
            error = Error(code=ERR_NOT_RUNNING,
                message="该会话未启动(可能启动失败),重新点进这个会话再发",
                msg_id=getattr(cmd, "msg_id", None))
            await self._emit_to_sid(sid, error)
            return error
        if getattr(cmd, "delivery", "immediate") != "immediate":
            return await self._enqueue_deferred_query(ctx, cmd)
        async with ctx.query_lock:
            return await self._handle_immediate_query(ctx, cmd)

    async def _handle_immediate_query(self, ctx: SessionContext, cmd):
        if ctx.engine == "codex":
            return await self._codex.start_turn(ctx, cmd)
        if (
            ctx.engine == "dsh"
            and ctx.dsh_pending_command_msg_id == getattr(cmd, "msg_id", None)
        ):
            # A reconnect may replay the same Query before the long-running
            # command settles. Its wrapper-owned task is already the at-most-once
            # owner, so acknowledge the duplicate by doing nothing.
            return None
        if ctx.state != "idle":
            error = Error(
                code=ERR_BUSY, message="该会话正忙,先 interrupt",
                msg_id=getattr(cmd, "msg_id", None))
            await self._emit(ctx, error)
            return error
        if ctx.engine == "dsh":
            if not cmd.prompt and not cmd.images and not cmd.files:
                error = Error(
                    code=ERR_BAD_PROMPT,
                    message="消息内容为空，请输入内容或添加图片。",
                    msg_id=getattr(cmd, "msg_id", None),
                )
                await self._emit(ctx, error)
                return error
            if cmd.files:
                error = Error(
                    code=ERR_BAD_PROMPT,
                    message="DeepSeek Harness 当前不支持通用文件附件。",
                    msg_id=getattr(cmd, "msg_id", None),
                )
                await self._emit(ctx, error)
                return error
            attachment_error = validate_attachments(cmd.images, None)
            if attachment_error:
                error = Error(
                    code=ERR_BAD_PROMPT,
                    message="图片附件不符合要求，请调整后重试。",
                    msg_id=getattr(cmd, "msg_id", None),
                )
                await self._emit(ctx, error)
                return error
            if not isinstance(ctx.sdk, DshSessionHandle):
                error = Error(
                    code=ERR_INTERNAL,
                    message="DeepSeek Harness 会话控制器不可用。",
                    msg_id=getattr(cmd, "msg_id", None),
                )
                await self._emit(ctx, error)
                return error
            if ctx.dsh_uncertain_steer_id is not None:
                error = Error(
                    code=ERR_STEER_UNKNOWN,
                    message=(
                        "上一条 DeepSeek Harness 引导结果仍在核对；"
                        "确认前请勿重复发送。"
                    ),
                    msg_id=cmd.msg_id,
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                    sid=self._ctx_wire_sid(ctx),
                )
                await self.transport.send(error)
                return error
            try:
                self._dsh_client_ready()
            except DshError:
                error = Error(
                    code=ERR_NOT_RUNNING,
                    message="DeepSeek Harness 本机服务未连接，本次未发送。",
                    msg_id=getattr(cmd, "msg_id", None),
                )
                await self._emit(ctx, error)
                return error
            command_descriptor = None
            if cmd.prompt.startswith("/") and not cmd.images:
                try:
                    command_descriptor = await ctx.sdk.resolve_command(
                        cmd.prompt
                    )
                except DshUnavailable:
                    error = Error(
                        code=ERR_NOT_RUNNING,
                        message=(
                            "DeepSeek Harness 命令目录暂不可用，"
                            "本次未发送。"
                        ),
                        msg_id=cmd.msg_id,
                    )
                    await self._emit(ctx, error)
                    return error
                except (DshRpcError, DshProtocolError) as exc:
                    log.warning(
                        "DSH command discovery failed",
                        session_id=self._ctx_wire_sid(ctx),
                        error_type=type(exc).__name__,
                    )
                    error = Error(
                        code=ERR_INTERNAL,
                        message=(
                            "DeepSeek Harness 命令目录不兼容或暂不可用，"
                            "本次未发送。"
                        ),
                        msg_id=cmd.msg_id,
                    )
                    await self._emit(ctx, error)
                    return error
            ctx.interrupt_event.clear()
            ctx.interrupt_deadline = None
            ctx.active_msg_id = cmd.msg_id
            await self._set_state(ctx, "running")
            if command_descriptor is not None:
                ctx.dsh_pending_command_msg_id = cmd.msg_id
                ctx.dsh_pending_command_prompt = cmd.prompt
                try:
                    await self._emit(ctx, ProcessEvent(
                        item_id=dsh_command_item_id(cmd.msg_id, cmd.msg_id),
                        kind="task",
                        phase="start",
                        status="running",
                        title=f"/{command_descriptor.name}"[:1024],
                        command=cmd.prompt,
                    ))
                except Exception as exc:
                    log.debug(
                        "DSH command start projection delayed",
                        session_id=self._ctx_wire_sid(ctx),
                        error_type=type(exc).__name__,
                    )
                ctx.turn_task = asyncio.create_task(
                    self._run_dsh_command(
                        ctx,
                        cmd.prompt,
                        cmd.msg_id,
                        command_descriptor.name,
                    ),
                    name=f"cc-remote-dsh-command-{cmd.msg_id}",
                )
                return None
            try:
                result = await ctx.sdk.prompt(
                    cmd.prompt,
                    images=getattr(cmd, "images", None),
                    rpc_id=cmd.msg_id,
                    mode="queue",
                )
            except DshRpcError as exc:
                ctx.active_msg_id = None
                await self._set_state(ctx, "idle")
                error = Error(
                    code=ERR_BAD_PROMPT,
                    message=_dsh_prompt_rejection_message(
                        exc,
                        fallback="DeepSeek Harness 拒绝了本次消息。",
                    ),
                    msg_id=cmd.msg_id,
                )
                await self._emit(ctx, error)
                return error
            except (DshUnavailable, DshProtocolError) as exc:
                # The carrier may fail after DSH appended the user message. Do
                # not mark the optimistic turn failed or accept a duplicate
                # retry until the source log proves which side committed.
                ctx.dsh_uncertain_msg_id = cmd.msg_id
                log.warning(
                    "DSH prompt receipt lost; reconciling source history",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )
                tracked = self._track_dsh_task(
                    self._reconcile_dsh_prompt(ctx, cmd.msg_id),
                    name=f"cc-remote-dsh-prompt-{cmd.msg_id}",
                )
                if not tracked:
                    await self._reconcile_dsh_prompt(ctx, cmd.msg_id)
                return None
            except Exception as exc:
                ctx.active_msg_id = None
                await self._set_state(ctx, "idle")
                log.warning(
                    "DSH prompt failed",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )
                error = Error(
                    code=ERR_INTERNAL,
                    message="DeepSeek Harness 消息未确认，请刷新历史后再重试。",
                    msg_id=cmd.msg_id,
                )
                await self._emit(ctx, error)
                return error
            if isinstance(result, dict) and isinstance(
                result.get("command"), dict
            ):
                # A registry change can race the preceding commands/list read.
                # session.prompt still reports the settled command but omits
                # commandId, so close the optimistic row honestly and let the
                # next canonical history read adopt the durable native id.
                command = result["command"]
                name = cmd.prompt[1:].split(maxsplit=1)[0] or "command"
                ctx.dsh_pending_command_msg_id = cmd.msg_id
                ctx.dsh_pending_command_prompt = cmd.prompt
                await self._finish_dsh_command(
                    ctx,
                    msg_id=cmd.msg_id,
                    prompt=cmd.prompt,
                    name=name,
                    kind="success",
                    text=command.get("text"),
                    command_id=None,
                )
            return None
        if ctx.engine == "claude" and ctx.space == "code":
            await self._adopt_claude_broker_handle(ctx)
            if ctx.state != "idle":
                error = Error(
                    code=ERR_BUSY,
                    message="该会话正由终端中的 Claude 回合运行，先 interrupt",
                    msg_id=getattr(cmd, "msg_id", None),
                )
                await self._emit(ctx, error)
                return error
        is_claude_broker = bool(getattr(ctx.sdk, "is_claude_broker", False))
        is_codex_shared = self._codex_shared_affinity(ctx)
        if (
            is_codex_shared
            and not await self._ensure_codex_daemon_generation(
                ctx, reason="query preflight")
        ):
            error = Error(
                code=ERR_NOT_RUNNING,
                message="Codex 共享通道重连失败，本次未发送；请重试",
                msg_id=getattr(cmd, "msg_id", None),
            )
            await self._emit(ctx, error)
            return error
        # DSH-only wrapper: the DeepSeek Harness query path above owns every
        # resident session; the legacy engine path is not reachable.
        error = Error(
            code=ERR_INTERNAL,
            message="该查询路径在当前 DSH Remote 中不可用。",
            msg_id=getattr(cmd, "msg_id", None),
        )
        await self._emit(ctx, error)
        return error
    async def _handle_steer(self, cmd):
        """Append one user instruction to the active DeepSeek Harness turn.

        Steering is neither a new engine turn nor an interrupt.  The successful
        narrative echo is replayable so every browser splits the visible task at
        the same point, while command failures remain correlated to the sender.
        """
        sid = getattr(cmd, "sid", None)
        ctx = self._ctx_for(sid)

        async def reject(code: str, message: str) -> Error:
            error = Error(
                code=code,
                message=message,
                msg_id=getattr(cmd, "msg_id", None),
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
                sid=self._ctx_wire_sid(ctx) if ctx is not None else sid,
            )
            # This is a correlated control rejection, not shared session
            # narrative. Buffering it would let a later client replay A's
            # targeted failure as its own after hello rewrites the recipient.
            await self.transport.send(error)
            return error

        if ctx is None:
            return await reject(
                ERR_NOT_STEERABLE, "该会话未启动，无法引导当前任务")
        if ctx.engine == "dsh":
            if ctx.state != "running":
                return await reject(
                    ERR_NOT_STEERABLE,
                    "当前没有可引导的 DeepSeek Harness 任务",
                )
            if cmd.files:
                return await reject(
                    ERR_NOT_STEERABLE,
                    "DeepSeek Harness 当前不支持通用文件附件。",
                )
            attachment_error = validate_attachments(
                getattr(cmd, "images", None), None
            )
            if attachment_error or (not cmd.prompt and not cmd.images):
                return await reject(
                    ERR_NOT_STEERABLE,
                    "消息内容或图片附件不符合要求。",
                )
            if not isinstance(ctx.sdk, DshSessionHandle):
                return await reject(
                    ERR_NOT_STEERABLE,
                    "DeepSeek Harness 会话控制器不可用。",
                )
            if ctx.dsh_uncertain_steer_id is not None:
                return await reject(
                    ERR_STEER_UNKNOWN,
                    "上一条 DeepSeek Harness 引导结果仍在核对；确认前请勿重复发送。",
                )
            try:
                async with ctx.steer_lock:
                    if ctx.state != "running":
                        return await reject(
                            ERR_NOT_STEERABLE,
                            "DeepSeek Harness 任务已结束，本次引导未发送。",
                        )
                    await ctx.sdk.prompt(
                        cmd.prompt,
                        images=getattr(cmd, "images", None),
                        rpc_id=cmd.msg_id,
                        mode="steer",
                    )
                ctx.active_msg_id = cmd.msg_id
                return None
            except DshRpcError as exc:
                return await reject(
                    ERR_NOT_STEERABLE,
                    _dsh_prompt_rejection_message(
                        exc,
                        fallback="DeepSeek Harness 拒绝了本次引导。",
                    ),
                )
            except (DshUnavailable, DshProtocolError) as exc:
                # The HTTP response can disappear after DSH appended the steer.
                # Correlate its rpcId against durable history before reporting
                # anything that might invite the user to duplicate it.
                ctx.dsh_uncertain_steer_id = cmd.msg_id
                log.warning(
                    "DSH steer receipt lost; reconciling source history",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )
                outcome = await self._reconcile_dsh_steer(
                    ctx, cmd.msg_id, notify_rejected=False,
                )
                if outcome is True:
                    return None
                if outcome is False:
                    return await reject(
                        ERR_NOT_STEERABLE,
                        "DeepSeek Harness 未接收本次引导。",
                    )
                return await reject(
                    ERR_STEER_UNKNOWN,
                    "DeepSeek Harness 尚未确认本次引导是否生效；"
                    "请先观察后续输出，确认前不要重复发送。",
                )
            except Exception as exc:
                log.warning(
                    "DSH steer failed",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )
                return await reject(
                    ERR_NOT_STEERABLE,
                    "DeepSeek Harness 引导未确认，请刷新历史后重试。",
                )
        # DSH-only wrapper: every resident session is a DeepSeek Harness turn
        # served by the branch above.
        return await reject(
            ERR_NOT_STEERABLE, "当前没有可引导的任务")

    async def _handle_interrupt(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "打断")
        if ctx.engine == "codex":
            return await self._codex.interrupt(ctx, cmd)
        # Reliable command retries and impatient second clicks are expected.
        # Once the first interrupt has been accepted, another stop must be an
        # idempotent no-op rather than a misleading `not_running` error that also
        # leaves the browser's state machine stuck in interrupting.
        if ctx.state in {"interrupting", "draining"}:
            return
        if ctx.state != "running":
            error = Error(
                code=ERR_NOT_RUNNING,
                message="该会话没有正在运行的回合",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error
        self._cancel_pending_asks(ctx)
        # Set the deadline and wake the turn consumer before entering any await:
        # it may already be blocked in the queue.get() that began while running.
        ctx.interrupt_deadline = (
            asyncio.get_running_loop().time() + self.cfg.drain_timeout
        )
        ctx.state = "interrupting"
        ctx.interrupt_event.set()
        log.info(
            "interrupt accepted",
            session_id=self._ctx_wire_sid(ctx),
            engine=ctx.engine,
            client_id=getattr(cmd, "client_id", None),
            cmd_id=getattr(cmd, "cmd_id", None),
        )
        await self._emit(ctx, StateEvent(state="interrupting"))
        if (
            ctx.engine == "dsh"
            and ctx.dsh_pending_command_msg_id is not None
        ):
            # commands/execute forwards HTTP cancellation to the command
            # handler's AbortSignal; session.cancel only addresses model turns.
            # Cancel the exact owning request instead of sending the wrong RPC.
            command_task = ctx.turn_task
            if command_task is not None and command_task is not asyncio.current_task():
                if not command_task.done():
                    command_task.cancel()
                await asyncio.gather(command_task, return_exceptions=True)
            elif ctx.dsh_pending_command_msg_id is not None:
                msg_id = ctx.dsh_pending_command_msg_id
                prompt = ctx.dsh_pending_command_prompt or ""
                await self._finish_dsh_command(
                    ctx,
                    msg_id=msg_id,
                    prompt=prompt,
                    name=(
                        prompt.removeprefix("/").split(maxsplit=1)[0]
                        or "command"
                    ),
                    kind="error",
                    text="命令已由 Remote 打断",
                    command_id=None,
                    interrupted=True,
                )
            return
        # A turn can still be reconnecting to apply effort/tier changes and may
        # not have submitted its query yet.  Serialize against that final launch
        # window: if the turn observes our event and aborts, it changes state to
        # idle; otherwise query() has returned and the new live turn is safe to
        # interrupt here.
        async with ctx.launch_lock:
            if ctx.state != "interrupting":
                return
            try:
                await ctx.sdk.interrupt()
            except Exception as e:
                log.exception("interrupt call failed", error=str(e))
                if ctx.engine == "dsh":
                    tracked = self._track_dsh_task(
                        self._reconcile_dsh_terminal(ctx),
                        name=(
                            "cc-remote-dsh-interrupt-"
                            f"{ctx.session_id or ctx.key}"
                        ),
                    )
                    if not tracked:
                        await self._reconcile_dsh_terminal(ctx)
                    return
                # The stream is still authoritative: a very fast turn may have
                # completed just before this RPC, with its terminal frame already
                # queued. Do not surface raw app-server text or force idle here.
                # The managed/spontaneous consumer drains that frame; if it never
                # arrives, the absolute interrupt deadline reconnects and unlocks.
                if ctx.turn_task is None and ctx.codex_spontaneous_task is None:
                    await self._set_state(ctx, "idle")
                return
            if ctx.state != "interrupting":
                return
            # The session.cancel RPC succeeded, but DSH 0.1.2 does not always
            # follow a cancelled turn with a turn/end frame on the follow
            # stream (the browser then stays stuck in interrupting and refuses
            # the next message).  Give the stream one scheduling turn to
            # deliver a terminal, then reconcile from the source log/catalog
            # so an actually-idle session returns to idle.
            if ctx.engine == "dsh":
                tracked = self._track_dsh_task(
                    self._reconcile_dsh_terminal_after_interrupt(ctx),
                    name=(
                        "cc-remote-dsh-interrupt-settle-"
                        f"{ctx.session_id or ctx.key}"
                    ),
                )
                if not tracked:
                    await self._reconcile_dsh_terminal_after_interrupt(ctx)

    @staticmethod
    def _dsh_models_payload(
        value: dict,
    ) -> tuple[list[dict], str | None, str | None]:
        groups = value.get("groups")
        current = value.get("current")
        if not isinstance(current, dict):
            # DSH 0.1.2 modelCatalog names the active selection `default`.
            current = value.get("default")
        if not isinstance(groups, list):
            raise DshProtocolError("DSH model catalog omitted groups")
        default_model = None
        default_effort = None
        current_provider = None
        current_model = None
        if isinstance(current, dict):
            provider = current.get("provider")
            model = current.get("model")
            if isinstance(provider, str) and isinstance(model, str):
                try:
                    default_model = encode_dsh_model(provider, model)
                except ValueError:
                    default_model = None
                current_provider = provider
                current_model = model
            selected_effort = current.get("reasoningEffort")
            if isinstance(selected_effort, str) and selected_effort:
                default_effort = selected_effort[:64]
        result: list[dict] = []
        for group in groups[:128]:
            if not isinstance(group, dict):
                continue
            provider = group.get("id")
            provider_name = group.get("name")
            models = group.get("models")
            if not isinstance(provider, str) or not isinstance(models, list):
                continue
            for model_row in models[:512]:
                if not isinstance(model_row, dict):
                    continue
                native_model = model_row.get("id")
                if not isinstance(native_model, str):
                    continue
                try:
                    wire_model = encode_dsh_model(provider, native_model)
                except ValueError:
                    continue
                reasoning = model_row.get("reasoning")
                raw_efforts = (
                    reasoning.get("efforts")
                    if isinstance(reasoning, dict) else []
                )
                efforts: list[str] = []
                if isinstance(raw_efforts, list):
                    for effort_row in raw_efforts[:64]:
                        effort_id = (
                            effort_row.get("id")
                            if isinstance(effort_row, dict) else None
                        )
                        if (
                            isinstance(effort_id, str)
                            and effort_id
                            and len(effort_id) <= 64
                        ):
                            efforts.append(effort_id)
                configured_effort = (
                    reasoning.get("defaultEffort")
                    if isinstance(reasoning, dict) else None
                )
                row: dict[str, object] = {
                    "id": wire_model,
                    "display_name": (
                        str(model_row.get("name") or native_model)[:256]
                    ),
                    "description": (
                        str(model_row.get("description"))[:4096]
                        if isinstance(model_row.get("description"), str)
                        else str(provider_name or provider)[:256]
                    ),
                    "efforts": efforts,
                    "is_default": (
                        provider == current_provider
                        and native_model == current_model
                    ),
                    "provider": provider[:256],
                    "provider_name": str(provider_name or provider)[:256],
                }
                if (
                    isinstance(configured_effort, str)
                    and configured_effort in efforts
                ):
                    row["default_effort"] = configured_effort
                result.append(row)
        return result, default_model, default_effort

    async def _handle_get_dsh_models(self, cmd) -> Models | Error:
        client_id = getattr(cmd, "client_id", None)
        try:
            client, _history = self._dsh_client_ready()
            requested_sid = getattr(cmd, "session_id", None)
            native_sid = None
            if requested_sid:
                native_sid = dsh_native_session_id(requested_sid)
            # DSH 0.1.2 exposes one model catalog (session/modelCatalog) for
            # both scoped and unscoped reads; the old llm.models Remote is gone.
            value = await client.call(
                "session.models", {"sessionId": native_sid} if native_sid else {}
            )
            if not isinstance(value, dict):
                raise DshProtocolError(
                    "DSH model catalog returned an invalid value"
                )
            models, default_model, default_effort = (
                self._dsh_models_payload(value)
            )
            ctx = self._ctx_by_sid(requested_sid) if requested_sid else None
            if (
                native_sid is not None
                and ctx is not None
                and ctx.engine == "dsh"
                and isinstance(ctx.sdk, DshSessionHandle)
            ):
                ctx.sdk.model = default_model
                ctx.sdk.effort = default_effort
            response = Models(
                engine="dsh",
                models=models,
                default_model=default_model,
                default_effort=default_effort,
                cwd=getattr(cmd, "cwd", None),
                session_id=requested_sid,
                to=client_id,
            )
            await self.transport.send(response)
            if requested_sid is not None and ctx is not None:
                route_id = getattr(cmd, "route_id", None)
                if default_model:
                    await self.transport.send(Model(
                        model=default_model,
                        sid=requested_sid,
                        to=client_id,
                        route_id=route_id,
                    ))
                if default_effort:
                    await self.transport.send(Effort(
                        effort=default_effort,
                        sid=requested_sid,
                        to=client_id,
                        route_id=route_id,
                    ))
            return response
        except Exception as exc:
            log.warning(
                "DSH model catalog failed",
                error_type=type(exc).__name__,
            )
            error = Error(
                code=ERR_INTERNAL,
                message="DeepSeek Harness 模型目录暂不可用，请稍后重试。",
                request_id=getattr(cmd, "cmd_id", None),
                to=client_id,
            )
            await self.transport.send(error)
            return error

    async def _handle_get_models(self, cmd) -> None:
        """Answer with the engine's catalog and effective new-session defaults."""
        engine = getattr(cmd, "engine", None) or "dsh"
        if engine == "dsh":
            return await self._handle_get_dsh_models(cmd)
        error = Error(
            code=ERR_AUTH,
            message="该引擎在当前 DSH Remote 中不可用。",
            request_id=getattr(cmd, "cmd_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self.transport.send(error)
        return error

    async def _runtime_control_preflight(
        self, ctx: SessionContext, *, action: str,
        request_id: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> Optional[Error]:
        """Bind a control to the session's real writer before changing state.

        A normal Claude CLI and the wrapper's Agent SDK can resume the same
        transcript at once.  Model/effort/permission controls must never mutate
        the SDK copy and announce success while the user's terminal owns another
        live TUI.  Prefer an exact broker session; otherwise use the same
        fail-closed ownership scan as Query before touching any runtime option.
        """
        if (ctx.engine == "claude" and ctx.space == "code"
                and not getattr(ctx.sdk, "is_claude_broker", False)):
            await self._adopt_claude_broker_handle(ctx)

        is_claude_broker = bool(
            getattr(ctx.sdk, "is_claude_broker", False))
        if is_claude_broker and ctx.state != "idle":
            error = Error(
                code=ERR_BUSY,
                message=f"Claude TUI 正在处理回合，完成或打断后再{action}",
                request_id=request_id,
                to=client_id,
            )
            await self._emit(ctx, error)
            return error

        if not ctx.session_id or is_claude_broker:
            return None
        if ctx.engine == "dsh":
            try:
                self._dsh_client_ready()
            except DshError:
                error = Error(
                    code=ERR_NOT_RUNNING,
                    message=f"DeepSeek Harness 本机服务未连接，无法{action}。",
                    request_id=request_id,
                    to=client_id,
                )
                await self._emit(ctx, error)
                return error
            return None
        if ctx.engine == "claude" and ctx.space != "code":
            return None
        if self._codex_shared_affinity(ctx):
            if await self._ensure_codex_daemon_generation(
                ctx, reason=f"runtime control preflight: {action}"
            ):
                return None
            error = Error(
                code=ERR_NOT_RUNNING,
                message=f"Codex 共享通道重连失败，无法{action}；请重试",
                request_id=request_id,
                to=client_id,
            )
            await self._emit(ctx, error)
            return error

        route_sid = self._ctx_wire_sid(ctx)
        if not route_sid:
            return None
        self._watch_session(route_sid)
        external = (
            await self._prime_codex_ownership(route_sid)
            if ctx.engine == "codex"
            else await self._prime_claude_ownership(route_sid)
        )
        if not external:
            return None

        await self._sync_external_control(
            ctx, self._watch.get(route_sid))
        engine_name = "Codex" if ctx.engine == "codex" else "Claude"
        guidance = (
            "请先点击『接管』，或退出 CLI 后再操作"
            if ctx.engine == "claude"
            else "请先退出终端或点击『接管』"
        )
        error = Error(
            code=ERR_BUSY,
            message=(f"该会话正由本机原生 {engine_name} CLI 控制，{action}未生效；"
                     f"{guidance}"),
            request_id=request_id,
            to=client_id,
        )
        await self._emit(ctx, error)
        return error

    async def _handle_get_engine_capabilities(self, cmd):
        engine = cmd.engine
        space = getattr(cmd, "space", "code")
        if engine == "dsh":
            client_id = getattr(cmd, "client_id", None)
            target_cwd = getattr(cmd, "cwd", None)
            requested_sid = getattr(cmd, "session_id", None)
            ctx = (
                self.sessions.get(requested_sid)
                if isinstance(requested_sid, str) else None
            )
            if ctx is not None and (
                ctx.engine != "dsh"
                or ctx.space != "code"
                or self._ctx_wire_sid(ctx) != requested_sid
            ):
                ctx = None
            items = []
            errors = []
            cwd_value = target_cwd or (ctx.cwd if ctx else getattr(self.cfg, "cc_cwd", None) or os.getcwd())
            if requested_sid is None:
                errors.append(
                    "请先打开一个 DeepSeek Harness 会话再读取 Skills"
                )
            elif ctx is None or not ctx.session_id:
                errors.append(
                    "DeepSeek Harness 会话未连接，请重新打开后读取 Skills"
                )
            elif target_cwd and os.path.realpath(target_cwd) != os.path.realpath(
                ctx.cwd
            ):
                errors.append(
                    "DeepSeek Harness 会话目录已变化，请刷新后重试"
                )
            else:
                try:
                    client, _history = self._dsh_client_ready()
                    value = await client.call(
                        "skill.list", {"sessionId": ctx.session_id}
                    )
                    rows = (
                        value.get("skills")
                        if isinstance(value, dict) else None
                    )
                    if not isinstance(rows, list):
                        raise DshProtocolError(
                            "skill.list returned an invalid value"
                        )
                    for row in rows[:2000]:
                        if not isinstance(row, dict):
                            continue
                        name = row.get("name")
                        description = row.get("description")
                        if not isinstance(name, str) or not name:
                            continue
                        detail = row.get("whenToUse")
                        items.append({
                            "kind": "skill",
                            "id": name[:512],
                            "name": name[:512],
                            "description": (
                                description[:16 * 1024]
                                if isinstance(description, str) else None
                            ),
                            "enabled": True,
                            "installed": True,
                            "status": (
                                "可由模型调用"
                                if row.get("modelInvocable") is True
                                else "仅用户调用"
                            ),
                            "scope": "project",
                            "source": "DeepSeek Harness",
                            "detail": (
                                detail[:4096]
                                if isinstance(detail, str) else None
                            ),
                            "actions": [],
                        })
                except Exception as exc:
                    log.warning(
                        "DSH skill catalog failed",
                        error_type=type(exc).__name__,
                    )
                    errors.append("DeepSeek Harness Skills 暂时不可用")
            result = EngineCapabilities(
                engine="dsh",
                space="code",
                request_id=getattr(cmd, "cmd_id", None),
                cwd=os.path.realpath(cwd_value),
                items=items,
                errors=errors,
                notes=[
                    "插件组合由 DSH Agent Preset 管理；"
                    "cc-remote 首版只读展示已组合的 Skills。"
                ],
                skills_only=getattr(cmd, "skills_only", False),
                session_id=requested_sid,
                to=client_id,
            )
            await self.transport.send(result)
            return result
        error = Error(
            code=ERR_AUTH,
            message="该引擎在当前 DSH Remote 中不可用。",
            request_id=getattr(cmd, "cmd_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self.transport.send(error)
        return error

    async def _handle_set_model(self, cmd):
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "切换模型")
        control_error = await self._runtime_control_preflight(
            ctx, action="切换模型")
        if control_error is not None:
            return control_error
        if ctx.engine == "dsh":
            if not isinstance(ctx.sdk, DshSessionHandle):
                error = Error(
                    code=ERR_INTERNAL,
                    message="DeepSeek Harness 会话控制器不可用。",
                )
                await self._emit(ctx, error)
                return error
            try:
                applied_model = await ctx.sdk.set_model(cmd.model)
                ctx.announced_model = applied_model
                events: list[object] = [Model(model=applied_model)]
                await self._emit(ctx, events[0])
                selected_effort = ctx.sdk.effort
                if isinstance(selected_effort, str) and selected_effort:
                    ctx.announced_effort = selected_effort
                    effort_event = Effort(effort=selected_effort)
                    await self._emit(ctx, effort_event)
                    events.append(effort_event)
                return tuple(events)
            except (DshError, ValueError) as exc:
                log.warning(
                    "DSH model switch failed",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )
                error = Error(
                    code=ERR_BAD_PROMPT,
                    message="DeepSeek Harness 模型切换未完成，请刷新目录后重试。",
                )
                await self._emit(ctx, error)
                return error
        try:
            if getattr(ctx.sdk, "is_claude_broker", False):
                confirmed = await self._confirm_claude_broker_model_switch(
                    ctx, cmd.model, getattr(cmd, "client_id", None))
                if not confirmed:
                    current = getattr(ctx.sdk, "model", None)
                    if isinstance(current, str) and current:
                        event = Model(model=current)
                        await self._emit(ctx, event)
                        return event
                    return None
                # The user may answer after the terminal exits or ownership is
                # replaced. Re-bind the command before touching a runtime.
                control_error = await self._runtime_control_preflight(
                    ctx, action="切换模型")
                if control_error is not None:
                    return control_error
            await ctx.sdk.set_model(cmd.model)
            await self._refresh_pending_claude_work_baseline(ctx)
            await self._persist_claude_session_controls(ctx)
            if ctx.engine == "codex":
                # Model and nullable effort form one authoritative app-server
                # settings snapshot. Resolution may await config/read or the
                # model catalog, so publish them together only after rechecking
                # that authority; emitting Model first can pair an old model
                # with a newer thread/settings effort during that await.
                responses: list[object] = []
                await self._publish_codex_model_effort(
                    ctx, force=True, published=responses)
                return tuple(responses)
            applied_model = getattr(ctx.sdk, "model", None) or cmd.model
            ctx.announced_model = applied_model
            model_event = Model(model=applied_model)
            await self._emit(ctx, model_event)
            return (model_event,)
        except Exception as e:
            log.exception("set_model failed", error=str(e))
            error = (
                self._claude_broker_control_error("切换模型", e)
                if getattr(ctx.sdk, "is_claude_broker", False)
                else Error(code=ERR_INTERNAL, message="模型切换未完成，请重试。")
            )
            await self._emit(ctx, error)
            return error

    async def _handle_set_effort(self, cmd):
        # cc SDK: effort is a spawn-time flag (--effort), so record it and let
        # _run_turn respawn-with-resume lazily at the next turn. A broker-owned
        # official TUI instead applies the native /effort command immediately.
        # codex: effort is a per-turn turn/start param, so the live session honors it
        # immediately — no reconnect needed.
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "切换思考强度")
        requested_engine = getattr(cmd, "engine", None)
        if (
            (requested_engine is not None and requested_engine != ctx.engine)
            or (ctx.engine == "dsh" and requested_engine != "dsh")
        ):
            error = Error(
                code=ERR_PROTOCOL,
                message="思考强度请求与当前会话引擎不匹配。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error
        control_error = await self._runtime_control_preflight(
            ctx, action="切换思考强度")
        if control_error is not None:
            return control_error
        if ctx.engine == "dsh":
            if not isinstance(ctx.sdk, DshSessionHandle):
                error = Error(
                    code=ERR_INTERNAL,
                    message="DeepSeek Harness 会话控制器不可用。",
                )
                await self._emit(ctx, error)
                return error
            try:
                applied = await ctx.sdk.set_effort(cmd.effort)
            except (DshError, ValueError) as exc:
                log.warning(
                    "DSH effort switch failed",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )
                error = Error(
                    code=ERR_BAD_PROMPT,
                    message="DeepSeek Harness 思考强度切换未完成，请重试。",
                )
                await self._emit(ctx, error)
                return error
            ctx.announced_effort = applied
            event = Effort(effort=applied)
            await self._emit(ctx, event)
            return event
        if ctx.engine == "codex":
            applied = await self._apply_codex_effort(ctx, cmd.effort) or cmd.effort
            ctx.announced_effort = applied
            event = Effort(effort=applied)
            await self._emit(ctx, event)
            log.info("effort set", sid=ctx.session_id, effort=applied,
                     requested=cmd.effort, engine=ctx.engine)
            return event
        if getattr(ctx.sdk, "is_claude_broker", False):
            try:
                await ctx.sdk.set_effort(cmd.effort)
            except Exception as e:
                log.exception("set_effort failed", error=str(e))
                error = self._claude_broker_control_error(
                    "切换思考强度", e)
                await self._emit(ctx, error)
                return error
            applied = getattr(ctx.sdk, "effort", None) or cmd.effort
            ctx.sdk.applied_effort = applied
        else:
            # Agent SDK applies effort only after its next lazy reconnect.
            # Do not mark it applied here or _run_turn will skip that reconnect.
            ctx.sdk.effort = cmd.effort
            applied = cmd.effort
            await self._persist_claude_session_controls(ctx)
        ctx.announced_effort = applied
        event = Effort(effort=applied)
        await self._emit(ctx, event)
        log.info("effort set", sid=ctx.session_id, effort=applied, engine=ctx.engine)
        return event

    async def _handle_set_perm(self, cmd):
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "切换权限模式")
        requested_engine = getattr(cmd, "engine", None)
        if (
            (requested_engine is not None and requested_engine != ctx.engine)
            or (ctx.engine == "dsh" and requested_engine != "dsh")
        ):
            error = Error(
                code=ERR_PROTOCOL,
                message="权限模式请求与当前会话引擎不匹配。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error
        control_error = await self._runtime_control_preflight(
            ctx,
            action="切换权限模式",
            request_id=getattr(cmd, "cmd_id", None),
            client_id=getattr(cmd, "client_id", None),
        )
        if control_error is not None:
            return control_error
        if ctx.engine == "dsh":
            if not isinstance(ctx.sdk, DshSessionHandle):
                error = Error(
                    code=ERR_INTERNAL,
                    message="DeepSeek Harness 会话控制器不可用。",
                )
                await self._emit(ctx, error)
                return error
            try:
                applied = await ctx.sdk.set_permission_mode(
                    cmd.mode,
                    rpc_id=(
                        getattr(cmd, "cmd_id", None)
                        or f"dsh-permission-{uuid4().hex}"
                    ),
                )
            except (DshError, ValueError) as exc:
                log.warning(
                    "DSH permission switch failed",
                    session_id=self._ctx_wire_sid(ctx),
                    error_type=type(exc).__name__,
                )
                error = Error(
                    code=ERR_BAD_PROMPT,
                    message="DeepSeek Harness 权限模式切换未完成，请重试。",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self._emit(ctx, error)
                return error
            ctx.announced_perm = applied
            event = _session_permission_event(ctx)
            if event is None:
                error = Error(
                    code=ERR_INTERNAL,
                    message="DeepSeek Harness 未返回切换后的权限状态。",
                )
                await self._emit(ctx, error)
                return error
            await self._emit(ctx, event)
            return event
        if ctx.space == "work" and ctx.engine == "codex" and cmd.mode != "never":
            error = Error(
                code=ERR_AUTH,
                message="Codex Work 权限由隔离工作区固定管理，不能升级为交互授权",
            )
            await self._emit(ctx, error)
            return error
        allowed = (CODEX_PERMISSION_MODES if ctx.engine == "codex"
                   else CLAUDE_PERMISSION_MODES)
        if cmd.mode not in allowed:
            log.warning("invalid permission mode", sid=ctx.session_id,
                        engine=ctx.engine, mode=cmd.mode)
            error = Error(
                code=ERR_INTERNAL,
                message=(f"{ctx.engine} 不支持权限模式 {cmd.mode!r}; "
                         f"可选: {', '.join(sorted(allowed))}"),
            )
            await self._emit(ctx, error)
            return error
        try:
            await ctx.sdk.set_permission_mode(cmd.mode)
            applied = (getattr(ctx.sdk, "approval", None)
                       or getattr(ctx.sdk, "permission_mode", None)
                       or cmd.mode)
            await self._persist_claude_session_controls(ctx)
            await self._persist_codex_session_controls(ctx)
            ctx.announced_perm = applied
            event = Perm(mode=applied)
            await self._emit(ctx, event)
            return event
        except Exception as e:
            log.exception("set_permission_mode failed", error=str(e))
            error = (
                self._claude_broker_control_error("切换权限模式", e)
                if getattr(ctx.sdk, "is_claude_broker", False)
                else Error(code=ERR_INTERNAL, message="权限模式切换未完成，请重试。")
            )
            await self._emit(ctx, error)
            return error

    async def _handle_get_account_usage(self, cmd):
        from .account_usage import account_usage
        return await account_usage(self, cmd)

    async def _handle_get_context(self, cmd):
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "读取上下文")
        if ctx.engine != "dsh":
            error = Error(
                code=ERR_AUTH,
                message="该引擎在当前 DSH Remote 中不可用。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error
        if not isinstance(ctx.sdk, DshSessionHandle):
            event = ContextReport(
                total_tokens=0,
                max_tokens=0,
                percentage=0.0,
                available=False,
                categories=[],
            )
            await self._emit(ctx, event)
            return event
        changed: set[str] = set()
        try:
            changed = await ctx.sdk.refresh_projections()
        except DshError as exc:
            # A mux frame or earlier history read may still hold a useful
            # last-wins projection.  Report that cache rather than turning
            # a transient loopback read failure into a global error banner.
            log.warning(
                "DSH context projection refresh failed",
                session_id=self._ctx_wire_sid(ctx),
                error_type=type(exc).__name__,
            )
        # Report the live permission preset alongside the context read.  DSH
        # 0.1.2 delivers the permissions projection on the control stream, so
        # this value is authoritative even when the read above changed nothing.
        permission = _session_permission_event(ctx)
        if permission is not None:
            ctx.announced_perm = permission.mode
            await self._emit(ctx, permission)
        pressure = ctx.sdk.context_pressure
        raw_used = pressure.get(
            "projectedTokens", pressure.get("pressureTokens")
        )
        raw_window = pressure.get("contextWindow")
        available = (
            isinstance(raw_used, int)
            and not isinstance(raw_used, bool)
            and raw_used >= 0
            and isinstance(raw_window, int)
            and not isinstance(raw_window, bool)
            and raw_window > 0
        )
        used = raw_used if available else 0
        window = raw_window if available else 0
        breakdown = ctx.sdk.context_breakdown
        categories = [
            {
                "name": name,
                "tokens": breakdown[field],
                "color": color,
            }
            for field, name, color in (
                ("systemTokens", "≈ 系统提示词", "#7B83D9"),
                ("toolsTokens", "≈ 工具", "#D39B4A"),
                ("messageTokens", "≈ 消息", "#3E9B82"),
            )
            if field in breakdown
        ]
        event = ContextReport(
            total_tokens=used,
            max_tokens=window,
            percentage=min(100.0, used / window * 100.0) if window else 0.0,
            available=None if available else False,
            model=ctx.sdk.model,
            is_auto_compact_enabled=None,
            categories=categories,
        )
        await self._emit(ctx, event)
        return event

    @staticmethod
    def _cancel_pending_asks(ctx: SessionContext) -> None:
        """Wake prompt handlers so interrupt can drain instead of waiting 30m."""
        for future in tuple(ctx.pending_asks.values()):
            if not future.done():
                future.set_exception(AskCancelled())

    async def _on_ask(
        self,
        ctx: SessionContext,
        question: str,
        options: list[dict[str, str]],
        *,
        header: str | None = None,
        allow_text: bool = False,
        secret: bool = False,
        multi_select: bool = False,
        timeout: float = 30 * 60,
        ask_id: str | None = None,
        to: str | None = None,
    ) -> str | list[str]:
        """Called by THIS ctx's in-process MCP server when the agent invokes
        `ask_user`. Emits AskUser on the ctx and blocks until AnswerQuestion.
        Runs in the ctx's reader task while its turn loop is blocked on
        receive_response(); other ctxs' turns are unaffected."""
        async with ctx.ask_lock:
            return await self._on_ask_locked(
                ctx,
                question,
                options,
                header=header,
                allow_text=allow_text,
                secret=secret,
                multi_select=multi_select,
                timeout=timeout,
                ask_id=ask_id,
                to=to,
            )

    async def _on_ask_optional(self, *args, **kwargs) -> str | list[str] | None:
        """Map a structured no-answer outcome for fail-closed integrations."""
        try:
            return await self._on_ask(*args, **kwargs)
        except AskSkipped:
            # A dismissal is a decision, not an unavailable surface: the caller
            # must delegate it rather than treat it as "no answer, so reject".
            raise
        except AskUnavailable:
            return None

    async def _handle_answer_question(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "回答交互问题")

        async def reject(code: str, message: str):
            error = Error(
                code=code,
                message=message,
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error

        fut = ctx.pending_asks.get(cmd.ask_id)
        if fut is None:
            log.warning("answer for unknown ask_id", ask_id=cmd.ask_id)
            return await reject(
                ERR_NOT_RUNNING, "该交互问题已经结束或不存在")
        if fut.done():
            log.warning("answer for already-done ask_id", ask_id=cmd.ask_id)
            return await reject(
                ERR_NOT_RUNNING, "该交互问题已经由其他客户端回答")

        if getattr(cmd, "outcome", "answer") == "skip":
            # Dismissal is never an implicit answer: hand the request back to
            # DSH (fail-closed for approvals) instead of picking for the user.
            fut.set_exception(AskSkipped())
            log.info("ask_user skipped", ask_id=cmd.ask_id)
            return None

        spec = ctx.pending_ask_specs.get(cmd.ask_id)
        if spec is None:
            return await reject(ERR_INTERNAL, "交互问题状态不完整，请重新操作")
        answer = cmd.answer
        labels = spec["labels"]
        if isinstance(answer, list):
            if not spec["multi_select"]:
                return await reject(ERR_BAD_PROMPT, "该问题不支持多选")
            if len(set(answer)) != len(answer):
                return await reject(ERR_BAD_PROMPT, "多选答案不能包含重复选项")
            normalized: str | list[str] = answer
            values = answer
        else:
            if not answer.strip():
                return await reject(ERR_BAD_PROMPT, "回答不能为空")
            normalized = [answer] if spec["multi_select"] else answer
            values = [answer]
        if not spec["allow_text"] and any(value not in labels for value in values):
            return await reject(ERR_BAD_PROMPT, "回答不属于该问题的可选项")

        # No await occurs between the done check and set_result: the event loop
        # makes this the single atomic winner when multiple clients race.
        fut.set_result(normalized)
        log.info("ask_user answered", ask_id=cmd.ask_id)
        return None

    async def _invalidate_session_list(
        self, engine: str, space: str = "code",
    ) -> SessionListInvalidated:
        """Broadcast only a read hint; browsers own the correlated refresh."""
        event = SessionListInvalidated(engine=engine, space=space)
        await self.transport.send(event)
        return event

    async def _handle_list_sessions(self, cmd) -> None:
        engine = getattr(cmd, "engine", "dsh")
        if engine == "dsh":
            return await self._list_dsh_sessions(cmd)
        error = Error(
            code=ERR_AUTH,
            message="该引擎在当前 DSH Remote 中不可用。",
            request_id=getattr(cmd, "cmd_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self.transport.send(error)
        return error

    async def _handle_switch_session(self, cmd) -> None:
        # Focus change — NO disconnect. If the session is already resident, just
        # focus it (its turn keeps running in the background). If not resident,
        # spawn (resume) it. The previously-focused session is NOT interrupted.
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        engine = getattr(cmd, "engine", None) or "dsh"
        if engine != "dsh":
            error = Error(
                code=ERR_AUTH,
                message="该引擎在当前 DSH Remote 中不可用。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        try:
            native_sid = dsh_native_session_id(sid)
        except ValueError:
            error = Error(
                code=ERR_AUTH,
                message="DeepSeek Harness 会话标识无效",
                request_id=getattr(cmd, "cmd_id", None),
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        ctx = self.sessions.get(sid)
        newly_spawned = ctx is None
        if ctx is None:
            ctx = await self._spawn(
                resume_id=native_sid, engine="dsh", space="code"
            )
            if ctx is None:
                # Surface it on the session the user switched INTO (not the stale
                # focused one). _spawn has already emitted the specific cause;
                # this durable session-scoped row only closes the loading state.
                error = Error(
                    code=ERR_CC_CRASH,
                    message="DeepSeek Harness 会话暂时无法打开，请稍后重试。",
                )
                await self._emit_to_sid(sid, error)
                return error
        self.focused_sid = ctx.key
        await self._emit(ctx, self._query_queue_state(ctx))
        # Switching into a blocked session must show what it is blocked on.
        await self._resend_pending_asks(ctx.key, getattr(cmd, "client_id", None))
        # A newly-spawned session isn't tracked by the client yet — send its
        # snapshot + full replay so the client builds a runtime for it (else the
        # client would show an empty/wrong view).
        snap = None
        if newly_spawned:
            snap = Snapshot(cc_session_id=ctx.key,
                            state=ctx.buffer.latest_state() or ctx.state,
                            tail_text=ctx.buffer.latest_tail_text(), cwd=ctx.cwd,
                            generation=self.instance_id,
                            control=self._session_control(ctx))
            await self._emit(ctx, snap)
        focus = SessionFocus(
            session_id=ctx.key or self.focused_sid or sid, cwd=ctx.cwd)
        await self._emit(ctx, focus)
        cached_responses = [snap, focus] if snap is not None else [focus]
        control_event = self._session_control(ctx)
        await self._emit(ctx, control_event)
        cached_responses.append(control_event)
        presentation_store = self._presentation_store_for_engine(ctx.engine)
        if not ctx.btw and presentation_store is not None:
            try:
                presentation = await asyncio.to_thread(
                    presentation_store.get,
                    ctx.engine,
                    self._ctx_wire_sid(ctx) or ctx.key,
                )
            except SessionPresentationStoreError:
                log.warning(
                    "session completion receipt could not be seeded",
                    session_id=self._ctx_wire_sid(ctx) or ctx.key,
                )
            else:
                completion_event = self._completion_state(presentation)
                await self._emit(ctx, completion_event)
                cached_responses.append(completion_event)
        permission_event = _session_permission_event(ctx)
        if permission_event is not None:
            ctx.announced_perm = permission_event.mode
            await self._emit(ctx, permission_event)
            cached_responses.append(permission_event)
        model = _session_model(ctx)
        if model:
            ctx.announced_model = model
            model_event = Model(model=model)
            await self._emit(ctx, model_event)
            cached_responses.append(model_event)
        effort = _session_effort(ctx)
        if effort:
            ctx.announced_effort = effort
            effort_event = Effort(effort=effort)
            await self._emit(ctx, effort_event)
            cached_responses.append(effort_event)
        state_event = await self._emit_current_state(ctx)
        cached_responses.append(state_event)
        return tuple(cached_responses)

    async def _handle_new_session(self, cmd) -> None:
        attachment_error = validate_attachments(
            getattr(cmd, "images", None), getattr(cmd, "files", None))
        if attachment_error:
            error = Error(
                code=ERR_BAD_PROMPT,
                message="附件不符合要求，请调整后重试。",
                request_id=getattr(cmd, "request_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        engine = getattr(cmd, "engine", "dsh")
        if engine != "dsh":
            error = Error(
                code=ERR_AUTH,
                message="该引擎在当前 DSH Remote 中不可用。",
                request_id=getattr(cmd, "request_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        try:
            ctx = await self._spawn(
                resume_id=None,
                cwd=getattr(cmd, "cwd", None),
                engine="dsh",
                model=getattr(cmd, "model", None),
                effort=getattr(cmd, "effort", None),
                dsh_agent_preset=getattr(cmd, "dsh_agent_preset", None),
                space="code",
                raise_on_failure=True,
            )
        except _SpawnFailure as exc:
            error = Error(
                code=exc.code,
                message=exc.message,
                request_id=getattr(cmd, "request_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        except Exception:
            log.exception("new DSH session spawn failed unexpectedly")
            error = Error(
                code=ERR_CC_CRASH,
                message="新会话暂时无法启动，请稍后重试。",
                request_id=getattr(cmd, "request_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        if ctx is None:
            error = Error(
                code=ERR_CC_CRASH,
                message="新会话启动失败，请检查 DSH 本机服务状态",
                request_id=getattr(cmd, "request_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        self.focused_sid = ctx.key
        # Establish the temp runtime's wrapper lifetime before any other
        # sequenced frame. Otherwise its cursor has no paired generation and a
        # normal transport reconnect is mistaken for a wrapper restart.
        snap = Snapshot(
            cc_session_id=ctx.key,
            state=ctx.state,
            tail_text=ctx.buffer.latest_tail_text(),
            cwd=ctx.cwd,
            generation=self.instance_id,
            control=self._session_control(ctx),
        )
        await self._emit(ctx, snap)
        # session_id is None until captured in _spawn_dsh; use the pool (temp)
        # key as the focus id — the client migrates to the real sid on capture.
        focus = SessionFocus(
            session_id=self.focused_sid,
            cwd=ctx.cwd,
            request_id=getattr(cmd, "request_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self._emit(ctx, focus)
        cached_responses = [snap, focus]
        initial_model = _session_model(ctx)
        if initial_model:
            ctx.announced_model = initial_model
            model_event = Model(model=initial_model)
            await self._emit(ctx, model_event)
            cached_responses.append(model_event)
        initial_effort = _session_effort(ctx)
        if initial_effort:
            ctx.announced_effort = initial_effort
            effort_event = Effort(effort=initial_effort)
            await self._emit(ctx, effort_event)
            cached_responses.append(effort_event)
        permission_event = _session_permission_event(ctx)
        if permission_event is not None:
            ctx.announced_perm = permission_event.mode
            await self._emit(ctx, permission_event)
            cached_responses.append(permission_event)
        prompt = getattr(cmd, "prompt", None)
        images = getattr(cmd, "images", None)
        files = getattr(cmd, "files", None)
        if prompt is not None or images or files:
            query_result = await self._handle_query(Query(
                sid=ctx.key,
                prompt=prompt or "",
                msg_id=cmd.msg_id,
                images=images,
                files=files,
            ))
            if getattr(query_result, "type", None):
                cached_responses.append(query_result)
        await self._invalidate_session_list("dsh", "code")
        return tuple(cached_responses)

    async def _handle_rename_session(self, cmd) -> None:
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        if sid.startswith("dsh@") or getattr(cmd, "engine", None) == "dsh":
            if not sid.startswith("dsh@") or getattr(
                cmd, "space", "code"
            ) != "code":
                error = Error(
                    code=ERR_AUTH,
                    message="会话不属于请求的 DeepSeek Harness Code 空间",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
            try:
                native_sid = dsh_native_session_id(sid)
                client, _history = self._dsh_client_ready()
                await client.call("session.rename", {
                    "sessionId": native_sid,
                    "title": cmd.title,
                })
                self._remember_notification_title(sid, cmd.title)
                await self._invalidate_session_list("dsh", "code")
                return await self._list_dsh_sessions(cmd)
            except Exception as exc:
                log.warning(
                    "DSH rename failed",
                    session_id=sid,
                    error_type=type(exc).__name__,
                )
                error = Error(
                    code=ERR_INTERNAL,
                    message="DeepSeek Harness 会话重命名未完成，请重试。",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
        # DSH-only wrapper: the DeepSeek Harness branch above owns every session.
        error = Error(
            code=ERR_AUTH,
            message="该操作在当前 DSH Remote 中不可用。",
            request_id=getattr(cmd, "cmd_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self.transport.send(error)
        return error

    async def _handle_archive_session(self, cmd) -> None:
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        if sid.startswith("dsh@") or getattr(cmd, "engine", None) == "dsh":
            if not sid.startswith("dsh@") or getattr(
                cmd, "space", "code"
            ) != "code":
                error = Error(
                    code=ERR_AUTH,
                    message="会话不属于请求的 DeepSeek Harness Code 空间",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
            if not cmd.archived:
                error = Error(
                    code=ERR_PROTOCOL,
                    message="当前 DeepSeek Harness API 尚未提供取消归档。",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
            try:
                native_sid = dsh_native_session_id(sid)
                client, _history = self._dsh_client_ready()
                value = await client.call("workspace.archiveSession", {
                    "sessionId": native_sid,
                })
                archived = (
                    value.get("archivedSessionIds")
                    if isinstance(value, dict) else None
                )
                if not isinstance(archived, list) or not all(
                    isinstance(item, str) for item in archived
                ):
                    raise DshProtocolError(
                        "workspace.archiveSession returned invalid state"
                    )
                # The receipt carries the authoritative archived roster; cache
                # it so catalog reads no longer need the removed workspace.list.
                self._dsh_archived_session_ids = set(archived)
                await self._invalidate_session_list("dsh", "code")
                return await self._list_dsh_sessions(cmd)
            except Exception as exc:
                log.warning(
                    "DSH archive failed",
                    session_id=sid,
                    error_type=type(exc).__name__,
                )
                error = Error(
                    code=ERR_INTERNAL,
                    message="DeepSeek Harness 会话归档未完成，请重试。",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
        # DSH-only wrapper: the DeepSeek Harness branch above owns every session.
        error = Error(
            code=ERR_AUTH,
            message="该操作在当前 DSH Remote 中不可用。",
            request_id=getattr(cmd, "cmd_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self.transport.send(error)
        return error

    async def _handle_pin_session(self, cmd) -> None:
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        if sid.startswith("dsh@") or getattr(cmd, "engine", None) == "dsh":
            pin_store = self._dsh_session_pins
            if (
                not sid.startswith("dsh@")
                or getattr(cmd, "space", "code") != "code"
                or pin_store is None
            ):
                error = Error(
                    code=ERR_AUTH if pin_store is not None else ERR_INTERNAL,
                    message=(
                        "会话不属于请求的 DeepSeek Harness Code 空间"
                        if pin_store is not None
                        else "置顶状态存储暂不可用"
                    ),
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
            try:
                native_sid = dsh_native_session_id(sid)
                rows, _archived = await self._read_dsh_catalog()
                if not any(
                    row.get("sessionId") == native_sid
                    and row.get("origin") != "subagent"
                    for row in rows
                ):
                    raise LookupError("DSH session does not exist")
                await asyncio.to_thread(
                    pin_store.set_pinned,
                    "dsh",
                    sid,
                    bool(cmd.pinned),
                )
                return await self._list_dsh_sessions(cmd)
            except Exception as exc:
                log.warning(
                    "DSH pin update failed",
                    session_id=sid,
                    error_type=type(exc).__name__,
                )
                error = Error(
                    code=ERR_INTERNAL,
                    message="DeepSeek Harness 置顶状态保存失败。",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
        is_codex = await self._is_codex_session(sid)
        engine = "codex" if is_codex else "claude"
        requested_engine = getattr(cmd, "engine", None)
        # DSH-only wrapper: the DeepSeek Harness branch above owns every session.
        error = Error(
            code=ERR_AUTH,
            message="该操作在当前 DSH Remote 中不可用。",
            request_id=getattr(cmd, "cmd_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self.transport.send(error)
        return error

    async def _handle_delete_session(self, cmd):
        """Delete one native session without confusing Code and Work roots."""
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        requested_engine = getattr(cmd, "engine", "dsh")
        if sid.startswith("dsh@") and requested_engine == "dsh":
            error = Error(
                code=ERR_AUTH,
                message=(
                    "DeepSeek Harness 当前未提供永久删除接口；"
                    "可以先归档该会话。"
                ),
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        error = Error(
            code=ERR_AUTH,
            message="该引擎在当前 DSH Remote 中不可用。",
            sid=sid,
            to=getattr(cmd, "client_id", None),
        )
        await self.transport.send(error)
        return error

    async def _send_session_fork_error(
        self, cmd, code: str, message: str,
    ) -> Error:
        client_id = getattr(cmd, "client_id", None)
        error = Error(
            code=code,
            message=message,
            request_id=getattr(cmd, "request_id", None),
            sid=getattr(cmd, "session_id", None),
            to=client_id,
        )
        if client_id:
            await self.transport.send(error)
        else:
            log.warning("dropping unroutable session fork error", code=code)
        return error

    async def _send_worktree_fork_error(
        self, cmd, code: str, message: str,
    ) -> Error:
        return await self._send_session_fork_error(cmd, code, message)

    async def _handle_fork_session(self, cmd):
        """Dispatch a message-level persistent fork to the owning engine."""
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        if sid.startswith("dsh@"):
            return await self._handle_dsh_fork_session(cmd, sid)
        is_codex = await self._is_codex_session(sid)
        engine = "codex" if is_codex else "claude"
        # Resident contexts already carry the authoritative surface.  Avoid an
        # unnecessary thread hop for Code: reliable Codex fork reconciliation
        # deliberately relies on there being no scheduling point between its
        # journal check and retry path.
        ctx = self._ctx_for(sid)
        is_work = ctx is not None and ctx.space == "work"
        if ctx is None:
            try:
                native_sid, work_profile_id = self._work_session_identity(
                    engine, sid)
            except ValueError:
                native_sid, work_profile_id = sid, None
            is_work = await asyncio.to_thread(
                self._work.for_engine(engine).get_by_session,
                native_sid,
                codex_profile_id=work_profile_id,
            ) is not None
        if is_work:
            return await self._send_session_fork_error(
                cmd, ERR_AUTH,
                "Work 会话不支持派生到 Code；请新建 Work 并选择同一项目")
        if is_codex:
            return await self._handle_codex_fork_session(cmd)
        return await self._handle_claude_fork_session(cmd, sid)

    async def _dsh_fork_candidate(
        self,
        entry: dict,
        *,
        attempts: int = 1,
    ) -> str | None:
        """Find the unique child created beyond one durable catalog cut."""
        baseline = set(entry.get("baseline_session_ids") or ())
        native_parent = entry.get("native_parent_session_id")
        at_seq = entry.get("at_seq")
        if not isinstance(native_parent, str) or not isinstance(at_seq, int):
            raise DshForkJournalError("DSH fork journal identity is invalid")
        client, _history = self._dsh_client_ready()
        for attempt in range(max(1, attempts)):
            if attempt:
                await asyncio.sleep(self.FORK_RECONCILE_DELAY)
            rows, _archived = await self._read_dsh_catalog()
            candidates = [
                row for row in rows
                if isinstance(row.get("sessionId"), str)
                and row["sessionId"] not in baseline
                and row.get("parentSessionId") == native_parent
                and row.get("origin") != "subagent"
            ]
            exact: list[str] = []
            for row in candidates[:32]:
                native_child = row["sessionId"]
                try:
                    page = await client.call("session.history", {
                        "sessionId": native_child,
                        # Slice at the durable fork cut instead of inspecting
                        # the child's moving tail. A child may have run many
                        # turns before a wrapper restart retries reconciliation;
                        # its inherited anchor must remain discoverable then.
                        "beforeSeq": at_seq + 1,
                        "maxMessages": 1,
                    })
                except DshError:
                    continue
                events = page.get("events") if isinstance(page, dict) else None
                if not isinstance(events, list):
                    continue
                if any(
                    isinstance(item, dict)
                    and isinstance(item.get("event"), dict)
                    and item["event"].get("type") == "turn/end"
                    and item["event"].get("seq") == at_seq
                    for item in events
                ):
                    exact.append(native_child)
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                raise DshForkJournalError(
                    "multiple DSH children match one uncertain fork"
                )
        return None

    async def _finish_dsh_fork(
        self,
        cmd,
        entry: dict,
        native_child: str,
    ) -> SessionForked:
        journal = self._dsh_forks
        if journal is None:
            raise DshForkJournalError("DSH fork journal is unavailable")
        child_sid = dsh_wire_session_id(native_child)
        await asyncio.to_thread(
            journal.complete, cmd.request_id, child_sid,
        )
        event = SessionForked(
            parent_session_id=entry["parent_session_id"],
            session_id=child_sid,
            cwd=entry["cwd"],
            target="same_cwd",
            last_turn_id=entry["last_turn_id"],
            request_id=cmd.request_id,
            to=getattr(cmd, "client_id", None),
        )
        # The read hint precedes navigation just like Claude/Codex forks.
        await self._invalidate_session_list("dsh", "code")
        await self.transport.send(event)
        try:
            await self._list_dsh_sessions(cmd)
        except Exception as exc:
            log.debug(
                "DSH fork sidebar refresh deferred",
                session_id=child_sid,
                error_type=type(exc).__name__,
            )
        return event

    async def _dsh_fork_candidate_or_defer(
        self,
        cmd,
        entry: dict,
        *,
        attempts: int,
    ) -> str:
        """Reconcile one submitted fork without acknowledging uncertainty."""
        try:
            child = await self._dsh_fork_candidate(
                entry,
                attempts=attempts,
            )
        except DshError as exc:
            log.info(
                "DSH fork reconciliation source unavailable",
                request_id=cmd.request_id,
                error_type=type(exc).__name__,
            )
            child = None
        if child is not None:
            return child
        await self._send_session_fork_error(
            cmd,
            ERR_FORK_RECONCILING,
            "DSH 派生结果仍在核对，请勿重复创建。",
        )
        raise _ForkOutcomeUncertain(
            "DSH fork outcome is not yet visible"
        )

    async def _handle_dsh_fork_session(self, cmd, sid: str):
        """Persistently fork one completed DSH turn exactly once."""
        if not getattr(cmd, "client_id", None):
            return await self._send_session_fork_error(
                cmd, ERR_AUTH, "派生会话需要已绑定的客户端"
            )
        journal = self._dsh_forks
        if journal is None:
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, "DSH 派生安全日志不可用，未执行派生"
            )
        request_id = cmd.request_id
        lock = self._dsh_fork_locks.get(request_id)
        if lock is None:
            if len(self._dsh_fork_locks) >= self.UNCERTAIN_FORK_CAP:
                return await self._send_session_fork_error(
                    cmd, ERR_BUSY, "DSH 派生请求过多，请稍后重试"
                )
            lock = asyncio.Lock()
            self._dsh_fork_locks[request_id] = lock
        terminal = False
        try:
            async with lock:
                try:
                    native_parent = dsh_native_session_id(sid)
                    at_seq = parse_dsh_fork_point(cmd.last_turn_id)
                except ValueError:
                    return await self._send_session_fork_error(
                        cmd, ERR_AUTH, "DeepSeek Harness 派生位置无效"
                    )
                try:
                    existing = await asyncio.to_thread(journal.get, request_id)
                    if existing is None:
                        rows, _archived = await self._read_dsh_catalog()
                        if len(rows) > 1000:
                            return await self._send_session_fork_error(
                                cmd,
                                ERR_INTERNAL,
                                "DSH 会话目录过大，当前版本无法安全派生",
                            )
                        parent = next((
                            row for row in rows
                            if row.get("sessionId") == native_parent
                            and row.get("origin") != "subagent"
                        ), None)
                        if parent is None:
                            return await self._send_session_fork_error(
                                cmd, ERR_NOT_RUNNING, "DeepSeek Harness 源会话不存在"
                            )
                        cwd = parent.get("cwd")
                        if not isinstance(cwd, str) or not cwd:
                            cwd = str(
                                (self._dsh_preflight.host
                                 if self._dsh_preflight else {}).get("cwd")
                                or os.getcwd()
                            )
                        cwd = os.path.realpath(cwd)
                        if not os.path.isdir(cwd):
                            return await self._send_session_fork_error(
                                cmd, ERR_INVALID_CWD, "源会话工作目录已不存在"
                            )
                        baseline = {
                            row["sessionId"] for row in rows
                            if isinstance(row.get("sessionId"), str)
                        }
                        existing = await asyncio.to_thread(
                            journal.begin,
                            request_id,
                            sid,
                            native_parent,
                            cmd.last_turn_id,
                            at_seq,
                            cwd,
                            baseline,
                        )
                    status_value = existing.get("status")
                    if status_value == "complete":
                        terminal = True
                        child_sid = existing.get("session_id")
                        if not isinstance(child_sid, str):
                            raise DshForkJournalError(
                                "completed DSH fork omitted child"
                            )
                        return await self._finish_dsh_fork(
                            cmd, existing, dsh_native_session_id(child_sid)
                        )
                    if status_value == "rejected":
                        terminal = True
                        return await self._send_session_fork_error(
                            cmd,
                            ERR_INTERNAL,
                            existing.get("error_message")
                            or "DeepSeek Harness 会话派生已被拒绝",
                        )
                    if status_value in {"submitted", "uncertain"}:
                        child = await self._dsh_fork_candidate_or_defer(
                            cmd,
                            existing,
                            attempts=4,
                        )
                        terminal = True
                        return await self._finish_dsh_fork(
                            cmd, existing, child,
                        )

                    claimed = await asyncio.to_thread(
                        journal.claim_submission, request_id,
                    )
                    if not claimed:
                        raise _ForkOutcomeUncertain(
                            "DSH fork submission ownership was lost"
                        )
                    try:
                        client, _history = self._dsh_client_ready()
                        result = await client.call("session.fork", {
                            "sessionId": native_parent,
                            "atSeq": at_seq,
                        })
                        if not isinstance(result, dict) or not isinstance(
                            result.get("sessionId"), str
                        ):
                            raise DshProtocolError(
                                "session.fork omitted child session id"
                            )
                        child = result["sessionId"]
                    except DshRpcError as exc:
                        partial_child = (
                            exc.details.get("sessionId")
                            if exc.code == "workspace-attach-failed" else None
                        )
                        if isinstance(partial_child, str):
                            child = partial_child
                        else:
                            message = (
                                exc.message[:512]
                                or "DeepSeek Harness 拒绝了会话派生"
                            )
                            await asyncio.to_thread(
                                journal.reject, request_id, message,
                            )
                            terminal = True
                            return await self._send_session_fork_error(
                                cmd, ERR_INTERNAL, message,
                            )
                    except (DshUnavailable, DshProtocolError):
                        await asyncio.to_thread(
                            journal.mark_uncertain, request_id,
                        )
                        child = await self._dsh_fork_candidate_or_defer(
                            cmd,
                            existing,
                            attempts=4,
                        )
                    terminal = True
                    return await self._finish_dsh_fork(cmd, existing, child)
                except DshForkJournalError as exc:
                    log.warning(
                        "DSH fork safety check failed",
                        request_id=request_id,
                        error_type=type(exc).__name__,
                    )
                    # A journal write can fail after ``submitted`` was durably
                    # recorded and after DSH created the child.  Returning a
                    # normal Error here would ACK the reliable command even
                    # though the at-most-once outcome still needs recovery.
                    # Keep it retryable/reconciling whenever the last readable
                    # state has crossed the native mutation boundary.
                    try:
                        pending = await asyncio.to_thread(
                            journal.get, request_id,
                        )
                    except DshForkJournalError:
                        pending = None
                    if pending is not None and pending.get("status") in {
                        "submitted", "uncertain",
                    }:
                        await self._send_session_fork_error(
                            cmd,
                            ERR_FORK_RECONCILING,
                            "DSH 派生结果仍在核对，请勿重复创建。",
                        )
                        raise _ForkOutcomeUncertain(
                            "DSH fork journal failed after submission"
                        ) from exc
                    return await self._send_session_fork_error(
                        cmd, ERR_INTERNAL, "DSH 派生状态无法安全确认，未重复执行"
                    )
        finally:
            keep_lock = False
            if not terminal:
                try:
                    pending = await asyncio.to_thread(journal.get, request_id)
                except DshForkJournalError:
                    pending = None
                keep_lock = bool(
                    pending is not None
                    and pending.get("status") in {"submitted", "uncertain"}
                )
            if not keep_lock and self._dsh_fork_locks.get(request_id) is lock:
                self._dsh_fork_locks.pop(request_id, None)

    async def _send_session_migration_error(
        self, cmd, code: str, message: str, *, sid: Optional[str] = None,
    ) -> Error:
        error = Error(
            code=code,
            message=message,
            request_id=getattr(cmd, "request_id", None),
            sid=sid or getattr(cmd, "session_id", None),
            to=getattr(cmd, "client_id", None),
        )
        ctx = self._ctx_by_sid(sid or getattr(cmd, "session_id", ""))
        if ctx is not None:
            await self._emit(ctx, error)
        else:
            await self.transport.send(error)
        return error

    async def _ensure_dsh_workspace(
        self, client: DshClient, cwd: str,
    ) -> str | None:
        """Create or resolve the DSH workspace that owns one directory.

        ``workspace.create`` is documented as create-or-resolve and is
        idempotent, so this also adopts a folder the desktop already opened.
        Failure is never fatal: an ungrouped session still works.
        """
        try:
            value = await client.call("workspace.create", {"path": cwd})
        except DshError as exc:
            log.warning(
                "DSH workspace registration failed",
                cwd=cwd,
                error_type=type(exc).__name__,
            )
            return None
        workspace = value.get("workspace") if isinstance(value, dict) else None
        workspace_id = (
            workspace.get("workspaceId") if isinstance(workspace, dict) else None
        )
        if not isinstance(workspace_id, str) or not workspace_id:
            log.warning("DSH workspace registration returned no id", cwd=cwd)
            return None
        log.info("DSH workspace resolved", cwd=cwd, workspace_id=workspace_id)
        return workspace_id

    async def _spawn_dsh(
        self,
        resume_id: Optional[str],
        *,
        cwd: Optional[str],
        model: Optional[str],
        effort: Optional[str],
        agent_preset: Optional[str],
        space: str,
        raise_on_failure: bool,
    ) -> Optional[SessionContext]:
        """Attach one DSH-owned session to the shared resident router.

        DSH keeps the Agent and log alive independently of cc-remote.  A
        resident context is therefore only a bounded control/event projection;
        eviction drops that projection without cancelling native work.
        """

        async def reject(code: str, message: str) -> None:
            if raise_on_failure:
                raise _SpawnFailure(code, message)
            target = None
            if resume_id:
                try:
                    target = dsh_wire_session_id(resume_id)
                except ValueError:
                    target = resume_id
            error = Error(code=code, message=message)
            if target:
                await self._emit_to_sid(target, error)
            else:
                await self._emit_focused(error)

        if space != "code":
            await reject(ERR_AUTH, "DeepSeek Harness 首版仅支持 Code 空间")
            return None
        try:
            client, history = self._dsh_client_ready()
        except DshError:
            await reject(
                ERR_NOT_RUNNING,
                "DeepSeek Harness 本机服务未连接，请先启动 dsh web。",
            )
            return None

        victim: str | None = None
        if len(self.sessions) >= self.cfg.max_concurrent_sessions:
            victim = next((
                key for key, candidate in self.sessions.items()
                if key != self.focused_sid
                and candidate.state == "idle"
                and not candidate.btw
                and not candidate.queued_queries
                and not self._query_queue_task_active(candidate)
            ), None)
            if victim is None:
                await reject(ERR_BUSY, "所有会话都在运行,先中断一个再切换")
                return None

        try:
            rows, _archived = await self._read_dsh_catalog()
            row = None
            native_sid = resume_id
            if native_sid is not None:
                if native_sid.startswith("dsh@"):
                    native_sid = dsh_native_session_id(native_sid)
                # Validate the native id even before trusting a catalog row.
                dsh_wire_session_id(native_sid)
                row = next((
                    candidate for candidate in rows
                    if candidate.get("sessionId") == native_sid
                    and candidate.get("origin") != "subagent"
                ), None)
                if row is None:
                    await reject(
                        ERR_NOT_RUNNING,
                        "DeepSeek Harness 会话不存在或不是可控制的主会话。",
                    )
                    return None

            if row is not None:
                recorded_cwd = row.get("cwd")
                target_cwd = (
                    os.path.realpath(recorded_cwd)
                    if isinstance(recorded_cwd, str) and recorded_cwd
                    else os.path.realpath(
                        str((self._dsh_preflight.host if self._dsh_preflight
                             else {}).get("cwd") or os.getcwd())
                    )
                )
                if not os.path.isdir(target_cwd):
                    await reject(
                        ERR_INVALID_CWD,
                        "DeepSeek Harness 会话的工作目录已不存在。",
                    )
                    return None
                resolved_preset = row.get("agentPreset")
                resolved_preset = (
                    resolved_preset
                    if isinstance(resolved_preset, str) else None
                )
            else:
                target_cwd = os.path.realpath(os.path.expanduser(
                    cwd or os.getcwd()
                ))
                if not os.path.isdir(target_cwd):
                    await reject(ERR_INVALID_CWD, f"目录不存在: {cwd}")
                    return None
                create_payload: dict[str, object] = {
                    # A known candidate makes create retries idempotent and lets
                    # us reconcile a response lost after native publication.
                    "sessionId": str(uuid4()),
                }
                # DSH's session.create takes workspaceId OR cwd, never both, and
                # only the workspace form accounts the session into the folder
                # grouping the DSH UI shows. A cwd-only session lands in 未分组
                # forever (no API can move it afterwards), so register the
                # directory first and fall back to cwd only if that fails.
                workspace_id = await self._ensure_dsh_workspace(
                    client, target_cwd)
                if workspace_id:
                    create_payload["workspaceId"] = workspace_id
                else:
                    create_payload["cwd"] = target_cwd
                if agent_preset:
                    create_payload["agentPreset"] = agent_preset
                candidate_sid = str(create_payload["sessionId"])
                try:
                    created = await client.call(
                        "session.create", create_payload
                    )
                except DshUnavailable:
                    # session.create documents retry-by-preallocated-id.  A
                    # post-commit carrier failure is resolved by an exact list
                    # witness instead of blindly creating a second session.
                    refreshed, _archived = await self._read_dsh_catalog()
                    row = next((
                        candidate for candidate in refreshed
                        if candidate.get("sessionId") == candidate_sid
                    ), None)
                    if row is None:
                        raise
                    created = {
                        "sessionId": candidate_sid,
                        "agentPreset": row.get("agentPreset"),
                    }
                if not isinstance(created, dict):
                    raise DshProtocolError(
                        "session.create returned an invalid value"
                    )
                native_sid = created.get("sessionId")
                if not isinstance(native_sid, str):
                    raise DshProtocolError("session.create omitted sessionId")
                dsh_wire_session_id(native_sid)
                resolved_preset = created.get("agentPreset")
                resolved_preset = (
                    resolved_preset
                    if isinstance(resolved_preset, str) else agent_preset
                )
                row = {"running": False}

            assert native_sid is not None
            handle = DshSessionHandle(
                client,
                native_sid,
                target_cwd,
                agent_preset=resolved_preset,
            )
            await handle.refresh_models()
            # DSH 0.2.0 keeps the selectable permission presets in a
            # process-level catalog, so a switcher has no options until it is
            # read.  A failure here must not block attaching the session.
            try:
                await handle.refresh_permission_catalog()
            except (DshError, ValueError) as exc:
                log.warning(
                    "DSH permission catalog read failed",
                    session_id=native_sid,
                    error_type=type(exc).__name__,
                )
            if model is not None:
                provider, native_model = decode_dsh_model(model)
                await handle.select_model(
                    provider, native_model, effort
                )
            elif effort is not None:
                await handle.set_effort(effort)
            wire_sid = dsh_wire_session_id(native_sid)
            ctx = SessionContext(
                session_id=native_sid,
                sdk=handle,
                buffer=RingBuffer(
                    self.cfg.ring_max_events,
                    self.cfg.ring_max_bytes,
                ),
                cwd=target_cwd,
                engine="dsh",
                space="code",
                state=(
                    "running"
                    if isinstance(row, dict) and row.get("running") is True
                    else "idle"
                ),
                dsh_agent_preset=resolved_preset,
            )
            ctx.key = wire_sid
            ctx.announced_model = handle.model
            ctx.announced_effort = handle.effort
            if victim is not None:
                def eligible(key: str, candidate: SessionContext) -> bool:
                    return bool(
                        key != self.focused_sid
                        and candidate.state == "idle"
                        and not candidate.btw
                        and not candidate.queued_queries
                        and not self._query_queue_task_active(candidate)
                    )

                current = self.sessions.get(victim)
                if current is None or not eligible(victim, current):
                    victim = next((
                        key for key, candidate in self.sessions.items()
                        if eligible(key, candidate)
                    ), None)
                if victim is not None:
                    previous = self.sessions.pop(victim)
                    self._purge_preview_image_snapshots(
                        previous.preview_snapshot_token
                    )
                    try:
                        await previous.sdk.disconnect()
                    except Exception:
                        pass
                    log.info("evicted idle session for DSH cap", key=victim)
                else:
                    # A previously-idle victim may start natively while DSH RPCs
                    # are in flight.  Never evict that now-running owner or lose
                    # the successfully-created DSH session; one lightweight DSH
                    # projection may temporarily exceed the residency target.
                    log.warning(
                        "DSH resident cap temporarily exceeded",
                        resident=len(self.sessions),
                        cap=self.cfg.max_concurrent_sessions,
                    )
            self.sessions[wire_sid] = ctx
            # Install an exact tail fold while live delivery waits on the same
            # lock. This closes the catalog→resident race for active sessions.
            async with ctx.dsh_event_lock:
                try:
                    # DSH 0.1.2 requires a throughSeq <= session cursor for the
                    # newest page; learn the cursor from a transient follow.
                    cursor = await client.fetch_session_cursor(native_sid)
                    page = await history.page(
                        native_sid,
                        wire_session_id=wire_sid,
                        before=None,
                        limit=4,
                        through_seq=cursor,
                    )
                except Exception as exc:
                    log.warning(
                        "DSH tail seed failed",
                        session_id=wire_sid,
                        error_type=type(exc).__name__,
                    )
                else:
                    handle.apply_projections(
                        page.projections, page.projection_seq,
                    )
                    ctx.dsh_translator = page.translator
                    ctx.dsh_last_source_seq = page.last_seq
            log.info(
                "DSH session attached",
                session_id=wire_sid,
                cwd=target_cwd,
                resident=len(self.sessions),
            )
            # DSH 0.1.2 delivers live turn events only through a per-session
            # follow stream on the multiplexed carrier.  Without it the wrapper
            # never sees turn/start..turn/end and a sent query hangs with the
            # browser showing an unfinished turn.
            try:
                await client.follow_session(native_sid)
            except Exception as exc:
                log.warning(
                    "DSH follow open failed",
                    session_id=wire_sid,
                    error_type=type(exc).__name__,
                )
            # Deliver any approval/question that arrived before this attach.
            await self._flush_pending_waterfalls(ctx)
            return ctx
        except _SpawnFailure:
            raise
        except (DshError, ValueError) as exc:
            log.warning(
                "DSH session attach failed",
                error_type=type(exc).__name__,
            )
            await reject(
                ERR_CC_CRASH,
                "DeepSeek Harness 会话连接未完成，请稍后重试。",
            )
            return None

    async def _spawn(self, resume_id: Optional[str], cwd: Optional[str] = None,
                     bootstrap: bool = False, engine: str = "claude",
                     codex_profile_id: Optional[str] = None,
                     model: Optional[str] = None, effort: Optional[str] = None,
                     collaboration_mode: Optional[str] = None,
                     permission_mode: Optional[str] = None,
                     permission_profile: Optional[str] = None,
                     web_search: Optional[str] = None,
                     service_tier: Optional[str] = None,
                     dsh_agent_preset: Optional[str] = None,
                     space: str = "code",
                     work_id: Optional[str] = None,
                     raise_on_failure: bool = False) -> Optional[SessionContext]:
        """Create a SessionContext, connect its SDK subprocess, load history.
        Returns the ctx (added to the pool under its real or temp key) or None
        on legacy-route failure (an Error has been emitted). NewSession uses
        ``raise_on_failure`` so its handler can send one correlated Error.
        `bootstrap` exempts the cap and
        retries resume→fresh on connect failure. `model`/`effort` (new_session
        only) pre-select those at spawn: effort BEFORE connect so the first turn
        runs at that strength with no respawn; cc model via a live set_model
        after connect; codex model as a per-turn field. An omitted Claude model
        resolves from current settings, then falls back to the curated default;
        omitted Codex controls retain native defaults."""
        if engine == "dsh":
            return await self._spawn_dsh(
                resume_id,
                cwd=cwd,
                model=model,
                effort=effort,
                agent_preset=dsh_agent_preset,
                space=space,
                raise_on_failure=raise_on_failure,
            )
        error = Error(
            code=ERR_AUTH,
            message="该引擎在当前 DSH Remote 中不可用。",
        )
        if raise_on_failure:
            raise _SpawnFailure(error.message)
        return None

    async def _next_from_queue(self, ctx: SessionContext, queue: asyncio.Queue):
        async def during_drain():
            deadline = ctx.interrupt_deadline
            remaining = (self.cfg.drain_timeout if deadline is None else
                         deadline - asyncio.get_running_loop().time())
            if remaining <= 0:
                try:
                    return queue.get_nowait()
                except asyncio.QueueEmpty as exc:
                    raise asyncio.TimeoutError from exc
            return await asyncio.wait_for(queue.get(), timeout=remaining)

        if ctx.state == "interrupting":
            return await during_drain()

        # A plain `await queue.get()` cannot notice a later interrupt. Race it
        # against the per-context event; if the event wins, keep waiting on the
        # queue only until the already-established absolute drain deadline.
        get_task = asyncio.create_task(queue.get())
        interrupt_task = asyncio.create_task(ctx.interrupt_event.wait())
        try:
            done, _ = await asyncio.wait(
                (get_task, interrupt_task), return_when=asyncio.FIRST_COMPLETED)
            if get_task in done:
                return get_task.result()
            get_task.cancel()
            await asyncio.gather(get_task, return_exceptions=True)
            return await during_drain()
        finally:
            for task in (get_task, interrupt_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get_task, interrupt_task, return_exceptions=True)
