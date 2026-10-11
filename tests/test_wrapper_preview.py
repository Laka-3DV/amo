"""Zero-token tests for the artifact preview / diff commands and the
"never leave a client waiting" rule.

Field failure these cover: tapping a changed file in the remote UI spun
forever. The DSH-only rewrite had removed both `get_file_preview` and
`get_diff`, and an unmapped command was answered with silence, so the browser's
correlated promise never settled.
"""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from cc_remote.protocol import (
    DiffReport, Error, FilePreview, GetDiff, GetFilePreview, SaveMarkdown,
)
from tests.test_multisession import _mk_ctx, _mk_machine


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Title\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    return tmp_path


def _preview_cmd(path: str, session_id: str = "dsh@s-1"):
    return GetFilePreview(
        path=path, request_id="req-preview", sid=session_id, client_id="client-1",
    )


@pytest.mark.asyncio
async def test_file_preview_returns_content_for_the_session_workspace(workspace):
    machine, transport = _mk_machine()
    machine.sessions["dsh@s-1"] = _mk_ctx("dsh@s-1")
    machine.sessions["dsh@s-1"].cwd = str(workspace)

    response = await machine._handle_get_file_preview(_preview_cmd("src/app.py"))

    assert isinstance(response, FilePreview)
    assert response.error is None
    assert response.format == "text"
    assert response.content == "print('hello')\n"
    assert response.request_id == "req-preview"
    assert response.to == "client-1"
    assert response.writable is True
    assert len(response.revision or "") == 64
    assert transport.sent[-1] is response, "the answer must actually be sent"


@pytest.mark.asyncio
async def test_file_preview_marks_markdown_and_refuses_escapes(workspace):
    machine, transport = _mk_machine()
    machine.sessions["dsh@s-1"] = _mk_ctx("dsh@s-1")
    machine.sessions["dsh@s-1"].cwd = str(workspace)

    markdown = await machine._handle_get_file_preview(_preview_cmd("README.md"))
    assert markdown.format == "markdown"

    escape = await machine._handle_get_file_preview(_preview_cmd("../../etc/passwd"))
    assert escape.error, "a path outside the session must be refused"
    assert escape.content == ""
    assert transport.sent[-1] is escape

    binary = await machine._handle_get_file_preview(_preview_cmd("blob.bin"))
    assert "二进制" in (binary.error or ""), binary.error


@pytest.mark.asyncio
async def test_file_preview_reports_when_no_session_is_resident(tmp_path):
    machine, transport = _mk_machine()
    response = await machine._handle_get_file_preview(_preview_cmd("a.py"))

    assert isinstance(response, FilePreview)
    assert response.error is not None
    assert transport.sent[-1] is response, "even a failure must be answered"


@pytest.mark.asyncio
async def test_file_preview_sends_images_as_base64(workspace):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AARAA"
        "B/gG0EQAAAABJRU5ErkJggg=="
    )
    (workspace / "dot.png").write_bytes(png)
    machine, _ = _mk_machine()
    machine.sessions["dsh@s-1"] = _mk_ctx("dsh@s-1")
    machine.sessions["dsh@s-1"].cwd = str(workspace)

    response = await machine._handle_get_file_preview(_preview_cmd("dot.png"))

    assert response.format == "image"
    assert response.media_type == "image/png"
    assert base64.b64decode(response.data or "") == png
    assert response.content == ""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ("git", *args), cwd=cwd, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"},
    )


@pytest.mark.asyncio
async def test_git_diff_covers_tracked_and_untracked_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "init")
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    (repo / "fresh.txt").write_text("brand new\n", encoding="utf-8")

    machine, transport = _mk_machine()
    machine.sessions["dsh@s-1"] = _mk_ctx("dsh@s-1")
    machine.sessions["dsh@s-1"].cwd = str(repo)

    await machine._handle_get_diff(
        GetDiff(file="tracked.txt", sid="dsh@s-1", client_id="client-1",
                cmd_id="cmd-1"))
    report = transport.sent[-1]
    assert isinstance(report, DiffReport)
    assert "-one" in report.diff and "+two" in report.diff
    assert report.request_id == "cmd-1"

    await machine._handle_get_diff(
        GetDiff(file="", sid="dsh@s-1", client_id="client-1", cmd_id="cmd-2"))
    whole = transport.sent[-1]
    assert isinstance(whole, DiffReport)
    assert "tracked.txt" in whole.diff
    assert "fresh.txt" in whole.diff, "untracked work must be visible too"


@pytest.mark.asyncio
async def test_unmapped_command_is_rejected_instead_of_hanging():
    machine, transport = _mk_machine()
    command = SaveMarkdown(
        path="a.md", content="x", expected_size=1, expected_mtime_ns="1",
        expected_revision="0" * 64, request_id="req-save", sid="dsh@s-1",
        client_id="client-1",
    )
    machine._command_router = _RouterReturningUnhandled()

    await machine._handle(command)

    error = transport.sent[-1]
    assert isinstance(error, Error)
    assert error.request_id == "req-save"
    assert error.sid == "dsh@s-1"
    assert error.to == "client-1"
    assert error.message


class _RouterReturningUnhandled:
    def __init__(self) -> None:
        from cc_remote.wrapper.command_router import UNHANDLED_COMMAND
        self._unhandled = UNHANDLED_COMMAND

    async def dispatch(self, command):
        return self._unhandled
