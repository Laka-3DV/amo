"""Bounded connection/bug sweep for the "反复重新连接" report.

A single "is it connected now?" check cannot see flapping: the link can drop and
recover between two checks and look perfectly healthy. Each round therefore also
counts reconnect and bug evidence in the wrapper log *for its own window*, and
repairs the layer that failed.

Rounds are bounded (default: every 30 minutes, 8 rounds ≈ 4 hours) and the sweep
retires itself when it is done, so a temporary investigation never becomes
permanent background noise.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

HOME = Path.home()
LOG_DIR = HOME / ".cc-remote" / "logs"
STATE_FILE = HOME / ".cc-remote" / "state" / "bugwatch.json"
REPORT_FILE = HOME / ".cc-remote" / "state" / "bugwatch-report.md"
RESULTS_LOG = HOME / ".cc-remote" / "logs" / "bugwatch.jsonl"
WRAPPER_LOG = LOG_DIR / "wrapper.log"
TUNNEL_LOG = LOG_DIR / "tunnel.log"

ROUND_INTERVAL_SECONDS = 1800
DEFAULT_ROUNDS = 8
#: Reconnects inside one window that count as flapping rather than a blip.
FLAP_THRESHOLD = 3
LOCK_LABEL = "io.amo.bugwatch"
TUNNEL_LABEL = "io.amo.tunnel"
WRAPPER_LABEL = "io.amo.connector"
DSH_URL = "http://127.0.0.1:3080"
RELAY_HEALTH = "http://127.0.0.1:8765/healthz"

#: wrapper-log signatures worth counting. Every one of these has been a real
#: reported bug at some point, so a nonzero count is evidence, not noise.
BUG_PATTERNS: dict[str, re.Pattern[str]] = {
    "relay_reconnects": re.compile(r'"msg": "relay connection lost'),
    "connections": re.compile(r'"msg": "connected to relay"'),
    "history_failed": re.compile(r'"msg": "DSH history failed'),
    "background_event_failed": re.compile(r'"msg": "DSH background event handling failed'),
    "unexpected_command": re.compile(r'"msg": "unexpected command"'),
    "event_dropped": re.compile(r'"msg": "DSH event dropped'),
    "preview_failed": re.compile(r'"msg": "file preview failed"'),
    "diff_failed": re.compile(r'"msg": "git diff failed"'),
    "attach_failed": re.compile(r'"msg": "DSH session attach failed"'),
    "interaction_capacity": re.compile(r'"msg": "DSH interaction capacity'),
    "held_replay": re.compile(r'"msg": "DSH interactive requests replayed'),
    "tunnel_exits": re.compile(r"tunnel exited"),
}

#: A nonzero count here is a bug the user feels; the rest are diagnostics.
BUG_ONLY = frozenset({
    "history_failed", "background_event_failed", "unexpected_command",
    "event_dropped", "preview_failed", "diff_failed", "attach_failed",
})


def parse_window_start(line: str) -> float | None:
    """Timestamp of one JSON log line, or None when it is not parseable."""
    if '"ts"' not in line:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    stamp = record.get("ts")
    if not isinstance(stamp, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return time.mktime(time.strptime(stamp, fmt))
        except ValueError:
            continue
    return None


def read_tail(path: Path, max_bytes: int = 4 * 1024 * 1024) -> list[str]:
    """Last ``max_bytes`` of a log, split into lines (never loads the whole file)."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()  # drop the partial first line
        return handle.read().decode("utf-8", errors="replace").splitlines()


def count_patterns(
    lines: Iterable[str],
    *,
    since: float,
    patterns: dict[str, re.Pattern[str]] | None = None,
) -> Counter[str]:
    """Count every signature in the lines whose timestamp is >= ``since``.

    An undated line cannot be attributed to a window, so it is ignored: the
    tunnel log used to be plain text, and counting its whole history as "this
    window" turned a four-day-old outage into a fresh flapping report.
    """
    table = BUG_PATTERNS if patterns is None else patterns
    counts: Counter[str] = Counter()
    for line in lines:
        stamp = parse_window_start(line)
        if stamp is None or stamp < since:
            continue
        for name, pattern in table.items():
            if pattern.search(line):
                counts[name] += 1
    return counts


def classify(counts: Counter[str]) -> tuple[list[str], list[str]]:
    """Split counters into problems worth acting on and plain observations."""
    problems: list[str] = []
    notes: list[str] = []
    for name in sorted(BUG_ONLY):
        if counts.get(name):
            problems.append(f"{name}={counts[name]}")
    reconnects = counts.get("relay_reconnects", 0)
    if reconnects >= FLAP_THRESHOLD:
        problems.append(f"反复重连={reconnects}")
    elif reconnects:
        notes.append(f"重连={reconnects}")
    for name in ("held_replay", "interaction_capacity", "tunnel_exits"):
        if counts.get(name):
            notes.append(f"{name}={counts[name]}")
    return problems, notes


def _http(url: str, timeout: float = 8.0) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"Cache-Control": "no-store"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:
        return 0, ""


#: DSH answering slower than this is a stall the user feels as a failed command.
DSH_SLOW_SECONDS = 3.0


def layer_status() -> dict[str, Any]:
    """One cheap probe per layer; values are what a human would check by hand."""
    code, body = _http(RELAY_HEALTH)
    relay_up = code == 200 and '"ok":true' in body
    started = time.monotonic()
    dsh_code = _http(DSH_URL, timeout=6.0)[0]
    dsh_seconds = round(time.monotonic() - started, 2)
    return {
        "tunnel": relay_up,
        "wrapper_connected": relay_up and '"wrapper_connected":true' in body,
        "clients": _client_count(body) if relay_up else 0,
        "dsh": dsh_code not in {0},
        "dsh_seconds": dsh_seconds,
    }


def wire_ok() -> tuple[bool, str]:
    """Whether the relay still accepts the dialect this client speaks."""
    from cc_remote.config import relay_config
    from cc_remote.protocol import PROTOCOL_VERSION
    from cc_remote.wirecheck import check

    try:
        cfg = relay_config()
        result = check(
            "http://127.0.0.1:8765", password=cfg.login_password,
            origin="http://127.0.0.1:8765", insecure=False,
            version=PROTOCOL_VERSION,
        )
    except Exception as exc:  # noqa: BLE001 - the failure is the finding
        return False, f"{type(exc).__name__}: {exc}"[:160]
    return result.ok, result.summary()


def _client_count(body: str) -> int:
    try:
        value = json.loads(body).get("clients")
    except Exception:
        return 0
    return value if isinstance(value, int) else 0


def _launchctl(*args: str) -> int:
    try:
        return subprocess.run(
            ("launchctl", *args), capture_output=True, timeout=20,
        ).returncode
    except Exception:
        return 1


def restart(label: str) -> bool:
    """Restart one LaunchAgent, reloading it when launchd has dropped it."""
    if _launchctl("kickstart", "-k", f"gui/{os.getuid()}/{label}") == 0:
        return True
    _launchctl("bootout", f"gui/{os.getuid()}/{label}")
    plist = HOME / "Library" / "LaunchAgents" / f"{label}.plist"
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist))
    return _launchctl("kickstart", "-k", f"gui/{os.getuid()}/{label}") == 0


def until_timestamp(value: str, now: float | None = None) -> float:
    """Next local occurrence of ``HH:MM`` (today if still ahead, else tomorrow)."""
    now = time.time() if now is None else now
    hour, _, minute = value.partition(":")
    target = time.localtime(now)
    stamp = time.mktime((
        target.tm_year, target.tm_mon, target.tm_mday,
        int(hour), int(minute), 0, 0, 0, -1,
    ))
    if stamp <= now:
        stamp += 24 * 3600
    return stamp


def log_line(record: dict[str, Any]) -> None:
    RESULTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_state(rounds: int) -> dict[str, Any]:
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        state = {}
    if not isinstance(state, dict) or state.get("rounds") != rounds:
        state = {"rounds": rounds, "completed": 0, "history": []}
    state.setdefault("history", [])
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def write_report(state: dict[str, Any]) -> None:
    lines = ["# bugwatch 报告（每 30 分钟一轮）", ""]
    for entry in state.get("history", []):
        stamp = time.strftime("%m-%d %H:%M", time.localtime(entry["at"]))
        verdict = "正常" if entry["ok"] else "异常"
        detail = "; ".join(entry.get("problems", [])) or "—"
        repairs = "; ".join(entry.get("repairs", [])) or "—"
        lines.append(
            f"- {stamp} 第{entry['round']}轮：{verdict}｜问题：{detail}"
            f"｜已修：{repairs}｜隧道:{'通' if entry['layers']['tunnel'] else '断'}"
            f" 设备:{'在线' if entry['layers']['wrapper_connected'] else '离线'}"
            f" 手机:{entry['layers']['clients']}"
        )
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines) + "\n")


def run_round(rounds: int, window: int = ROUND_INTERVAL_SECONDS) -> dict[str, Any]:
    """One sweep: probe the layers, read the window, repair what failed."""
    state = load_state(rounds)
    round_number = int(state["completed"]) + 1
    now = time.time()
    since = float(state.get("lastRunAt") or (now - window))

    layers = layer_status()
    wrapper_counts = count_patterns(read_tail(WRAPPER_LOG), since=since)
    tunnel_counts = count_patterns(read_tail(TUNNEL_LOG), since=since)
    problems, notes = classify(wrapper_counts)
    if tunnel_counts.get("tunnel_exits"):
        problems.append(f"隧道重启={tunnel_counts['tunnel_exits']}")
    if not layers["tunnel"]:
        problems.append("隧道不通")
    if layers["tunnel"] and not layers["wrapper_connected"]:
        problems.append("wrapper 未连上中继")
    if not layers["dsh"]:
        problems.append("DSH web 无响应")
    elif layers["dsh_seconds"] >= DSH_SLOW_SECONDS:
        problems.append(f"DSH 响应变慢 {layers['dsh_seconds']}s")

    wire_healthy, wire_summary = wire_ok()
    if not wire_healthy:
        problems.append("中继与客户端协议不匹配")
        notes.append(wire_summary)

    repairs: list[str] = []
    if problems:
        if not layers["tunnel"]:
            if restart(TUNNEL_LABEL):
                repairs.append("重启隧道")
        elif not layers["wrapper_connected"] or any(
                name in problems for name in ("relay_reconnects", "反复重连")) or any(
                p.startswith("反复重连") for p in problems):
            if restart(WRAPPER_LABEL):
                repairs.append("重启 wrapper")
        elif any(p.startswith("反复重连") for p in problems):
            if restart(TUNNEL_LABEL):
                repairs.append("重启隧道（抖动）")
        time.sleep(6)
        layers = layer_status()

    ok = not problems or bool(repairs and layers["wrapper_connected"]
                              and layers["tunnel"])
    entry = {
        "round": round_number,
        "at": int(now),
        "ok": ok,
        "problems": problems,
        "notes": notes,
        "repairs": repairs,
        "layers": layers,
        "counts": dict(wrapper_counts),
    }
    state["completed"] = round_number
    state["lastRunAt"] = int(now)
    state["history"].append(entry)
    state["done"] = round_number >= rounds
    state["nextCheckAt"] = int(now + window)
    save_state(state)

    RESULTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if state["done"]:
        write_report(state)
        # Retire the temporary sweep so it cannot become background noise.
        _launchctl("bootout", f"gui/{os.getuid()}/{LOCK_LABEL}")
    return entry


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    rounds = DEFAULT_ROUNDS
    window = ROUND_INTERVAL_SECONDS
    if "--rounds" in args:
        rounds = int(args[args.index("--rounds") + 1])
    if "--interval" in args:
        window = int(args[args.index("--interval") + 1])
    until = None
    if "--until" in args:
        until = until_timestamp(args[args.index("--until") + 1])
    if "--supervise" in args:
        # macOS on this machine does not honour StartInterval (a 60-second test
        # job never fired), so the sweep supervises itself: one long-lived
        # process under KeepAlive runs the rounds and then retires.
        while True:
            state = load_state(rounds)
            finished = int(state.get("completed", 0)) >= rounds
            expired = until is not None and time.time() >= until
            if finished or expired:
                if expired and not state.get("done"):
                    state["done"] = True
                    state["stoppedAt"] = int(time.time())
                    save_state(state)
                    write_report(state)
                    log_line({
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "level": "INFO", "msg": "bugwatch stopped at --until",
                        "rounds": state.get("completed", 0),
                    })
                time.sleep(6 * 3600)  # retired: idle instead of exiting (KeepAlive)
                continue
            run_round(rounds, window)
            time.sleep(window)
    if "--status" in args:
        state = load_state(rounds)
        done = int(state.get("completed", 0))
        left = max(0, int(state.get("nextCheckAt", 0) - time.time()))
        stop_note = ""
        if until is not None:
            stop_note = (
                "，停止时间 "
                + time.strftime("%H:%M", time.localtime(until))
            )
        print(f"bugwatch：已完成 {done}/{rounds} 轮"
              f"，距下一轮 {left // 60} 分钟{stop_note}")
        for entry in state.get("history", []):
            stamp = time.strftime("%m-%d %H:%M", time.localtime(entry["at"]))
            print(f"  第{entry['round']}轮 {stamp} "
                  f"{'正常' if entry['ok'] else '异常'} "
                  f"{'; '.join(entry['problems']) or ''}")
        return 0
    entry = run_round(rounds, window)
    print(json.dumps(entry, ensure_ascii=False))
    return 0 if entry["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI
    raise SystemExit(main())
