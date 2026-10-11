"""Wire conformance check between the browser and the relay.

A protocol change has two ends. Adding a field on the Mac side while the relay
still runs the old schema turns every send of that frame into a rejected frame
that the browser keeps replaying from its outbox — measured in the field as an
endless reconnect storm ("选择后发不出去，然后不断重连，就连不上").

This check speaks the *current* client dialect over the real relay and asserts
the connection survives: a mismatch is reported as a problem in minutes instead
of being discovered by the user at night.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import Any

PROBE_CLIENT_ID = "wirecheck"


@dataclass
class WireCheckResult:
    ok: bool
    relay: str
    survivors: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    detail: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        if self.ok:
            return f"wire OK（{len(self.survivors)} 种帧形状被接受）"
        return "wire 不匹配：" + "; ".join(self.failures)


def _login(base: str, password: str, origin: str, *, insecure: bool) -> str:
    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/login",
        data=json.dumps({"password": password}).encode(),
        headers={"Content-Type": "application/json", "Origin": origin},
    )
    with urllib.request.urlopen(request, timeout=15, context=context) as response:
        cookie = response.headers.get("set-cookie", "")
    if "cc_remote_session" not in cookie:
        raise RuntimeError(f"login rejected ({cookie!r})")
    return cookie.split(";")[0]


def current_client_frames() -> dict[str, dict[str, Any]]:
    """The frame shapes this build's browser actually sends."""
    return {
        # answer_question: an answer, and a dismissal that omits `answer`.
        "answer_question(answer+outcome)": {
            "type": "answer_question", "ask_id": "wirecheck-ask",
            "answer": "允许一次", "outcome": "answer", "sid": "wirecheck",
        },
        "answer_question(skip, no answer)": {
            "type": "answer_question", "ask_id": "wirecheck-ask",
            "outcome": "skip", "sid": "wirecheck",
        },
        "get_history": {
            "type": "get_history", "session_id": "wirecheck",
            "detail": "summary", "limit": 1,
        },
        "list_sessions": {"type": "list_sessions", "engine": "dsh"},
    }


async def _probe(raw_frames: dict[str, dict[str, Any]], *, base: str,
                 origin: str, cookie: str, insecure: bool,
                 version: int) -> tuple[list[str], list[str], dict[str, str]]:
    import websockets

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = ws_url.rstrip("/") + "/ws?machine=mac-local"
    ssl_context = ssl.create_default_context()
    if insecure:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    survivors: list[str] = []
    failures: list[str] = []
    detail: dict[str, str] = {}
    for index, (name, frame) in enumerate(raw_frames.items()):
        payload = {
            "v": version, "client_id": PROBE_CLIENT_ID,
            "cmd_id": f"wirecheck-{index}", **frame,
        }
        kwargs: dict[str, Any] = {
            "additional_headers": {
                "Cookie": cookie, "Origin": origin,
            },
            "open_timeout": 15,
            "proxy": None,
        }
        if ws_url.startswith("wss://"):
            kwargs["ssl"] = ssl_context
        try:
            async with websockets.connect(ws_url, **kwargs) as socket:
                await socket.send(json.dumps({
                    "v": version, "type": "hello", "role": "client",
                    "client_id": PROBE_CLIENT_ID, "machine_id": "mac-local",
                }))
                await socket.send(json.dumps(payload))
                try:
                    await asyncio.wait_for(socket.recv(), timeout=5)
                except asyncio.TimeoutError:
                    pass  # no reply needed: staying open is the assertion
                survivors.append(name)
        except Exception as exc:  # noqa: BLE001 - the failure IS the result
            failures.append(f"{name} 被拒绝")
            detail[name] = f"{type(exc).__name__}: {exc}"[:200]
    return survivors, failures, detail


def check(base: str, *, password: str, origin: str, insecure: bool,
          version: int) -> WireCheckResult:
    cookie = _login(base, password, origin, insecure=insecure)
    survivors, failures, detail = asyncio.run(_probe(
        current_client_frames(), base=base, origin=origin, cookie=cookie,
        insecure=insecure, version=version,
    ))
    return WireCheckResult(
        ok=not failures, relay=base, survivors=survivors,
        failures=failures, detail=detail,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cc_remote.wirecheck",
        description="检查中继是否接受当前客户端方言（协议两端是否同步）",
    )
    parser.add_argument("--relay", default="http://127.0.0.1:8765")
    parser.add_argument("--origin", default=None, help="默认与 --relay 相同")
    parser.add_argument("--password", default=None, help="默认读 LOGIN_PASSWORD")
    parser.add_argument("--insecure", action="store_true")
    args = parser.parse_args(argv)

    from cc_remote.config import relay_config
    from cc_remote.protocol import PROTOCOL_VERSION

    cfg = relay_config()
    password = args.password or cfg.login_password
    origin = args.origin or args.relay
    result = check(
        args.relay, password=password, origin=origin,
        insecure=args.insecure or args.relay.startswith("https://"),
        version=PROTOCOL_VERSION,
    )
    print(result.summary())
    for name, why in result.detail.items():
        print(f"  {name}: {why}")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI
    raise SystemExit(main())
