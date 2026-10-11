"""Bounded local Codex app-server JSON-RPC over stdio.

No model credentials or arbitrary server payloads enter the relay. Losing a
request response is not proof that a turn was rejected: mutations are never
replayed by this transport.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import os
import logging
import sys
import re
import ipaddress
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from cc_remote import __version__
from .child_env import sanitized_child_env

log = logging.getLogger(__name__)


def system_proxy_env(output: str, env: dict[str, str]) -> dict[str, str]:
    """Respect explicit environment settings; inherit enabled Mac loopback proxies.

    LaunchAgents do not inherit a terminal's HTTP(S)_PROXY, and reqwest does
    not read macOS System Configuration. Read only the global, enabled local
    proxy endpoints, never credentials, PAC scripts or scoped configurations.
    """
    if any(env.get(key) for key in ("ALL_PROXY", "all_proxy")):
        return {}
    fields = dict(re.findall(r"^  ([A-Za-z]+)\s*:\s*([^\n]+)$", output, re.MULTILINE))
    result = {}
    for scheme in ("HTTP", "HTTPS"):
        if any(env.get(key) for key in (scheme+"_PROXY", scheme.lower()+"_proxy")):
            continue
        if fields.get(scheme+"Enable", "").strip() != "1":
            continue
        host = fields.get(scheme+"Proxy", "").strip()
        try:
            local = host == "localhost" or ipaddress.ip_address(host).is_loopback
            port = int(fields.get(scheme+"Port", "0"))
        except ValueError:
            continue
        if local and 0 < port < 65536:
            address = f"[{host}]" if ":" in host else host
            result[scheme+"_PROXY"] = f"http://{address}:{port}"
    return result


async def inherit_macos_proxy(env: dict[str, str]) -> None:
    if sys.platform != "darwin":
        return
    probe = None
    try:
        probe = await asyncio.create_subprocess_exec(
            "/usr/sbin/scutil", "--proxy", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env)
        output, _ = await asyncio.wait_for(probe.communicate(), 2)
        if probe.returncode == 0 and len(output) <= 64 * 1024:
            env.update(system_proxy_env(output.decode("utf-8", "replace"), env))
    except (OSError, TimeoutError):
        pass
    finally:
        if probe and probe.returncode is None:
            probe.kill()
            await probe.wait()


def resolve_codex_binary(executable: str) -> str | None:
    # Use the desktop's matching CLI when installed. A global npm CLI can lag
    # behind the desktop's models and paginated-history format. An explicit
    # CC_REMOTE_CODEX_BIN always wins; never change the user's Codex config.
    if executable == "codex":
        for path in (
            Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
            Path("/Applications/Codex.app/Contents/Resources/codex"),
        ):
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    binary = shutil.which(executable)
    if not binary and executable == "codex":
        candidates = [Path.home()/".bun/bin/codex", Path.home()/".local/bin/codex",
                      Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")]
        binary = next((str(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
    return binary


class CodexUnavailable(Exception):
    pass


class CodexRpcError(Exception):
    def __init__(self, code: int | str, message: str):
        self.code = code
        super().__init__(message[:4096])


class CodexClient:
    def __init__(self, executable: str = "codex", timeout: float = 15):
        self.executable = executable
        self.timeout = timeout
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue(maxsize=2048)
        self.event_bytes = 0
        self.tasks: list[asyncio.Task] = []
        self.write_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.on_event: Callable[[dict], Awaitable[None]] | None = None
        self.on_disconnect: Callable[[], Awaitable[None]] | None = None
        self.ready = False
        self.stopping = False

    async def start(self, on_event, on_disconnect=None) -> None:
        async with self.start_lock:
            if self.ready:
                return
            if self.process is not None:
                # Reconnect reads/control without ever replaying a mutation.
                await self.stop()
            binary = resolve_codex_binary(self.executable)
            if not binary:
                raise CodexUnavailable("本机未找到 Codex CLI")
            env = sanitized_child_env()
            # A wrapper launched by an agent must not impersonate that agent's
            # desktop session or carry its private originator override.
            for key in ("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
                env.pop(key, None)
            await inherit_macos_proxy(env)
            paths = env.get("PATH", "").split(os.pathsep)
            for directory in (str(Path(binary).parent), str(Path.home()/".bun/bin"),
                              str(Path.home()/".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"):
                if directory not in paths and Path(directory).is_dir():
                    paths.append(directory)
            env["PATH"] = os.pathsep.join(paths)
            self.on_event, self.on_disconnect = on_event, on_disconnect
            self.stopping = False
            self.process = await asyncio.create_subprocess_exec(
                binary, "app-server", "--listen", "stdio://",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env,
                limit=16 * 1024 * 1024,
            )
            self.tasks = [asyncio.create_task(self._read()),
                          asyncio.create_task(self._consume()),
                          asyncio.create_task(self._drain_stderr())]
            try:
                await self.call("initialize", {"clientInfo": {
                    "name": "amo", "title": "Amo", "version": __version__,
                }, "capabilities": {"experimentalApi": True}})
                await self.write({"method": "initialized"})
                self.ready = True
            except BaseException:
                await self.stop()
                raise

    async def write(self, value: dict) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()+b"\n"
        if len(data) > 16 * 1024 * 1024:
            raise CodexUnavailable("Codex 请求超过大小限制")
        async with self.write_lock:
            process = self.process
            if process is None or process.returncode is not None or process.stdin is None:
                raise CodexUnavailable("Codex 本机进程未连接")
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise CodexUnavailable("Codex 本机连接已断开") from exc

    async def call(self, method: str, params: dict | None = None, *, rpc_id: str | None = None) -> dict:
        if len(self.pending) >= 256:
            raise CodexUnavailable("Codex 请求过多，请稍后重试")
        identity = rpc_id or uuid4().hex
        if identity in self.pending:
            raise CodexUnavailable("Codex 请求正在处理")
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.write({"id": identity, "method": method, "params": params or {}})
            return await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except TimeoutError as exc:
            log.warning("Codex RPC timed out: method=%s", method[:128])
            raise CodexUnavailable("Codex 请求结果尚未确认") from exc
        finally:
            self.pending.pop(identity, None)
            if not future.done():
                future.cancel()

    async def respond(self, identity: str | int, result: dict) -> None:
        await self.write({"id": identity, "result": result})

    async def reject(self, identity: str | int) -> None:
        await self.write({"id": identity, "error": {"code": -32601, "message": "Unsupported client request"}})

    async def _read(self) -> None:
        try:
            assert self.process and self.process.stdout
            while raw := await self.process.stdout.readline():
                if len(raw) > 16 * 1024 * 1024:
                    raise CodexUnavailable("Codex 事件超过大小限制")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    continue
                if "method" not in value and "id" in value:
                    future = self.pending.get(str(value["id"]))
                    if future and not future.done():
                        if "error" in value:
                            error = value["error"]
                            future.set_exception(CodexRpcError(error.get("code", -1), str(error.get("message", "RPC rejected"))))
                        else:
                            future.set_result(value.get("result") or {})
                elif "method" in value:
                    if self.events.full() or self.event_bytes+len(raw)>32*1024*1024:
                        raise CodexUnavailable("Codex 事件积压，连接已停止以避免丢失内容")
                    self.event_bytes += len(raw)
                    self.events.put_nowait((value, len(raw)))
        except (ValueError, OSError, CodexUnavailable):
            pass
        finally:
            self.ready = False
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(CodexUnavailable("Codex 本机连接已断开"))
            if not self.stopping:
                await self.events.put((None, 0))

    async def _consume(self) -> None:
        while True:
            value, size = await self.events.get()
            self.event_bytes -= size
            if value is None:
                if self.on_disconnect:
                    await self.on_disconnect()
                return
            try:
                if self.on_event:
                    await self.on_event(value)
            except Exception as exc:
                # A malformed display event must not terminate RPC processing.
                # Server-originated requests fail closed rather than hanging.
                log.warning("Codex event projection failed: method=%s error_type=%s",
                            str(value.get("method", ""))[:128], type(exc).__name__)
                if "id" in value:
                    await self.reject(value["id"])

    async def _drain_stderr(self) -> None:
        assert self.process and self.process.stderr
        while await self.process.stderr.read(8192):
            pass  # local diagnostic output can contain secrets; never relay it

    async def stop(self) -> None:
        self.stopping = True
        self.ready = False
        process = self.process
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.kill()
                await process.wait()
        current = asyncio.current_task()
        for task in self.tasks:
            if task is not current:
                task.cancel()
        await asyncio.gather(*(t for t in self.tasks if t is not current), return_exceptions=True)
        self.tasks.clear()
        self.process = None
        self.events = asyncio.Queue(maxsize=2048)
        self.event_bytes = 0
