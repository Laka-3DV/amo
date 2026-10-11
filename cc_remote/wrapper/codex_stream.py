"""Whitelist Codex display items into the existing engine-neutral wire format."""
from __future__ import annotations

import base64
import os
import stat
import hashlib

from cc_remote.attachments import MAX_SINGLE_ATTACHMENT_BYTES, validate_attachments

from cc_remote.protocol import (
    AssistantMsgStart, AssistantMsgEnd, Delta, ProcessEvent,
    ToolUse, ToolResult, UserMsg, TurnEnd, TurnResult, Error, ERR_CC_CRASH,
)
from .history_store import _history_image_refs
from .sanitize import bounded_tool_input, bounded_text

MODEL_UNAVAILABLE = "当前 Codex 模型不可用，请选择本机支持的模型后重试。"


def turn_failure(turn: dict) -> str:
    # Never send provider JSON, account information, headers or arbitrary error
    # text to clients. Classify locally into reviewed product messages only.
    error = turn.get("error") or {}
    message = str(error.get("message", "") if isinstance(error, dict) else error).lower()
    info = error.get("codexErrorInfo", error.get("codex_error_info")) if isinstance(error, dict) else None
    kind = str(info).lower()
    if "model" in message and any(word in message for word in ("not supported", "not found", "not available")):
        return MODEL_UNAVAILABLE
    if any(word in kind for word in ("unauthorized", "authentication")) or "401" in message or "403" in message:
        return "模型服务认证已失效或当前账号无权限，请检查当前服务的凭据或账号权限后重试。"
    if "ratelimit" in kind or "429" in message:
        return "请求过于频繁或当前额度受限，请稍后重试。"
    if "timeout" in kind or "timed out" in message:
        return "请求超时，请重新尝试。"
    return "本次回复未完成，请重试。"


def user_images(item: dict) -> list[dict]:
    images = []
    for part in item.get("content", [])[:128]:
        if not isinstance(part, dict) or len(images) >= 8:
            continue
        image = None
        if part.get("type") == "image":
            url = part.get("url")
            if not isinstance(url, str) or len(url) > MAX_SINGLE_ATTACHMENT_BYTES * 4 // 3 + 128:
                continue
            prefix, separator, data = url.partition(",")
            if separator and prefix.startswith("data:image/") and prefix.endswith(";base64"):
                image = {"media_type": prefix[5:-7], "data": data}
        elif part.get("type") == "localImage":
            path = part.get("path")
            # Resolve only a native user-image item, never a browser path. Open
            # regular files without following final symlinks or blocking on a
            # device/FIFO; image signature and pixel limits apply afterwards.
            if not isinstance(path, str) or not os.path.isabs(path):
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    meta = os.fstat(stream.fileno())
                    if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_SINGLE_ATTACHMENT_BYTES:
                        continue
                    raw = stream.read(MAX_SINGLE_ATTACHMENT_BYTES + 1)
                mime = "image/png" if raw.startswith(b"\x89PNG") else "image/jpeg" if raw.startswith(b"\xff\xd8") else "image/webp"
                image = {"media_type": mime, "data": base64.b64encode(raw).decode("ascii")}
            except (OSError, ValueError):
                continue
        if image and validate_attachments(images + [image], None) is None:
            images.append(image)
    return images


def image_id(turn_id: str, index: int, image: dict) -> str:
    # Bind locators to bytes as well as position. A local image may be replaced
    # or removed after sending; an old locator must fail, never show a different
    # file or slide to the next image in the message.
    digest = hashlib.sha256(f"{turn_id}\0{index}\0{image['media_type']}\0".encode())
    digest.update(image["data"].encode("ascii"))
    return "img-" + digest.hexdigest()[:24]


def image_refs(turn_id: str, images: list[dict]) -> list[dict]:
    refs = _history_image_refs(turn_id, images)
    for index, ref in enumerate(refs):
        ref["image_id"] = image_id(turn_id, index, images[index])
    return refs


def item_events(item: dict, *, completed: bool = True, user_id: str | None = None) -> list:
    identity = str(item.get("id", ""))[:256]
    if not identity:
        return []
    kind = item.get("type")
    text = lambda value: bounded_text(value, 65536)[0]
    if kind == "userMessage":
        prompt = "\n".join(part.get("text", "") for part in item.get("content", [])
                           if isinstance(part, dict) and part.get("type") == "text")
        turn_id = user_id or identity
        # Summaries and detail pages carry opaque locators; base64 is fetched
        # separately so reopening an image-heavy conversation stays bounded.
        refs = image_refs(turn_id, user_images(item))
        return [UserMsg(msg_id=turn_id, client_msg_id=item.get("clientId"), prompt=prompt[:128*1024],
                        image_refs=refs or None)]
    if kind == "agentMessage":
        channel = "commentary" if item.get("phase") == "commentary" else "final"
        rows = [AssistantMsgStart(message_id=identity, channel=channel)]
        if completed:
            rows += [Delta(message_id=identity, text=text(item.get("text", "")), channel=channel),
                     AssistantMsgEnd(message_id=identity, channel=channel)]
        return rows
    if kind in {"reasoning", "plan"}:
        # Native reasoning stores visible summary/content as strings. Never
        # serialize arbitrary objects or try to decode encrypted reasoning.
        def visible(value):
            if isinstance(value, str):
                return value[:65536]
            if isinstance(value, list):
                return "\n".join(part for part in value[:128] if isinstance(part, str))[:65536]
            return ""
        summary = visible(item.get("summary")) or visible(item.get("content")) or visible(item.get("text"))
        if kind == "reasoning" and completed and not summary:
            summary = "本机 Codex 未提供这段思考正文"
        return [ProcessEvent(item_id=identity, kind="reasoning" if kind=="reasoning" else "plan",
                             phase="end" if completed else "start", status="succeeded" if completed else "running",
                             title="思考" if kind=="reasoning" else "计划",
                             summary=summary)]
    names = {"commandExecution": "命令", "fileChange": "文件改动", "mcpToolCall": "工具调用",
             "dynamicToolCall": "工具调用", "webSearch": "搜索", "imageView": "查看图片",
             "collabAgentToolCall": "协作任务", "contextCompaction": "上下文整理"}
    if kind not in names:
        # Unknown objects can contain credentials, system prompts or plug-in
        # state. Render a generic marker; never forward their raw payload.
        return [ProcessEvent(item_id=identity, kind="task", phase="end" if completed else "start",
                             status="succeeded" if completed else "running", title="Agent 活动")]
    if kind == "commandExecution":
        payload = {"command": text(item.get("command", "")), "cwd": text(item.get("cwd", ""))}
        tool, category = "command", "command"
        output = text(item.get("aggregatedOutput", ""))
    elif kind == "fileChange":
        changes = item.get("changes", [])
        payload = {"file_paths": [str(c.get("path", ""))[:4096] for c in changes[:32] if isinstance(c, dict)]}
        tool, category = "apply_patch", "file"
        output = "\n".join(text(c.get("diff", "")) for c in changes[:32] if isinstance(c, dict))[:65536]
    elif kind in {"mcpToolCall", "dynamicToolCall"}:
        payload = bounded_tool_input(item.get("arguments", {}), 16384)
        tool, category = str(item.get("tool") or names[kind])[:256], "tool"
        output = text(item.get("error") or item.get("result") or item.get("contentItems", ""))
    else:
        payload = {k: text(item[k]) for k in ("query", "path", "prompt") if k in item}
        tool, category, output = names[kind], "tool", ""
    rows = [ToolUse(message_id=identity, tool_use_id=identity, tool=tool, category=category, input=payload, title=names[kind])]
    if completed:
        failed = item.get("status") in {"failed", "declined"} or item.get("exitCode") not in {None, 0} or bool(item.get("error"))
        rows.append(ToolResult(tool_use_id=identity, content=output, is_error=failed,
                               diff=output if kind == "fileChange" else None, truncated=len(output)>=65536,
                               status="failed" if failed else "succeeded"))
    return rows


def terminal_event(turn: dict) -> TurnEnd:
    status = turn.get("status", "failed")
    result = TurnEnd(turn_id=str(turn["id"]), result=TurnResult(
        subtype=status, duration_ms=max(0, int(turn.get("durationMs") or 0)), is_error=status!="completed"))
    result._codex_authoritative_terminal = True
    return result


def history_events(turn: dict, *, client_msg_id: str | None = None) -> list[dict]:
    rows = []
    for item in turn.get("items", []):
        if not isinstance(item, dict):
            continue
        for event in item_events(item, user_id=str(turn["id"]) if item.get("type")=="userMessage" else None):
            if isinstance(event, UserMsg) and client_msg_id:
                event.client_msg_id = client_msg_id
            rows.append(event.model_dump(mode="json"))
    if turn.get("status") != "inProgress":
        if turn.get("status") == "failed":
            rows.append(Error(code=ERR_CC_CRASH, message=turn_failure(turn), msg_id=str(turn["id"])).model_dump(mode="json"))
        rows.append(terminal_event(turn).model_dump(mode="json"))
    return rows
