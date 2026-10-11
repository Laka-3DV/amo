"""Bugwatch log analysis: the sweep must see flapping, not just "up now"."""
from __future__ import annotations

import json
import time
from collections import Counter

from cc_remote import bugwatch


def line(ts: str, msg: str) -> str:
    return json.dumps({"ts": ts, "level": "WARNING", "msg": msg})


NOW = time.time()
RECENT = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - 60))
OLD = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - 12 * 3600))


def test_counts_only_events_inside_the_window():
    lines = [
        line(OLD, "relay connection lost, reconnecting"),
        line(RECENT, "relay connection lost, reconnecting"),
        line(RECENT, "connected to relay"),
        "not json at all",
    ]
    counts = bugwatch.count_patterns(lines, since=NOW - 1800)

    assert counts["relay_reconnects"] == 1, "an old outage is not this window's flap"
    assert counts["connections"] == 1
    assert sum(counts.values()) == 2


def test_flapping_is_a_problem_but_a_single_blip_is_not():
    quiet = Counter({"relay_reconnects": 1, "connections": 2})
    problems, notes = bugwatch.classify(quiet)
    assert problems == []
    assert any("重连" in note for note in notes)

    flapping = Counter({
        "relay_reconnects": bugwatch.FLAP_THRESHOLD,
        "connections": bugwatch.FLAP_THRESHOLD,
    })
    problems, _ = bugwatch.classify(flapping)
    assert any("反复重连" in problem for problem in problems), problems


def test_known_bugs_are_reported_by_name():
    counts = Counter({
        "history_failed": 2,
        "unexpected_command": 4,
        "preview_failed": 1,
        "held_replay": 3,
    })
    problems, notes = bugwatch.classify(counts)

    assert "history_failed=2" in problems
    assert "unexpected_command=4" in problems
    assert "preview_failed=1" in problems
    assert any("held_replay=3" in note for note in notes), (
        "a replay is diagnostic, not a bug on its own"
    )


def test_undated_lines_are_never_attributed_to_a_window():
    """The tunnel log was plain text: its whole history looked like "now"."""
    counts = bugwatch.count_patterns(
        ["tunnel exited (255), retrying in 5s: Mon Oct 4 16:38:11"],
        since=time.time() - 60,
    )
    assert counts["tunnel_exits"] == 0

    dated = json.dumps({
        "ts": RECENT, "level": "WARNING", "msg": "tunnel exited", "code": 255,
    })
    counts = bugwatch.count_patterns([dated], since=NOW - 1800)
    assert counts["tunnel_exits"] == 1


def test_report_lists_every_round(tmp_path, monkeypatch):
    monkeypatch.setattr(bugwatch, "REPORT_FILE", tmp_path / "report.md")
    state = {
        "rounds": 8,
        "completed": 2,
        "history": [
            {"round": 1, "at": int(NOW), "ok": True, "problems": [], "repairs": [],
             "layers": {"tunnel": True, "wrapper_connected": True, "clients": 1}},
            {"round": 2, "at": int(NOW), "ok": False,
             "problems": ["反复重连=4"], "repairs": ["重启 wrapper"],
             "layers": {"tunnel": True, "wrapper_connected": True, "clients": 0}},
        ],
    }
    bugwatch.write_report(state)

    text = (tmp_path / "report.md").read_text()
    assert "第1轮" in text and "第2轮" in text
    assert "反复重连=4" in text
    assert "重启 wrapper" in text


def test_wirecheck_covers_the_frames_that_broke_the_client():
    """The check must speak the exact shapes that caused the reconnect storm."""
    from cc_remote.wirecheck import current_client_frames

    frames = current_client_frames()
    answer = frames["answer_question(answer+outcome)"]
    assert answer["outcome"] == "answer" and answer["answer"]

    skip = frames["answer_question(skip, no answer)"]
    assert skip["outcome"] == "skip"
    assert "answer" not in skip, (
        "a skip with an empty answer is an invalid frame: the relay closes the "
        "socket and the outbox replays it forever"
    )


def test_wirecheck_result_reports_failures_by_name():
    from cc_remote.wirecheck import WireCheckResult

    ok = WireCheckResult(ok=True, relay="http://r", survivors=["a"])
    assert "OK" in ok.summary()
    bad = WireCheckResult(ok=False, relay="http://r", failures=["a 被拒绝"])
    assert "不匹配" in bad.summary() and "a 被拒绝" in bad.summary()
