"""Zero-token, session-scoped browsing and bounded filename search."""
from pathlib import Path

import pytest

from cc_remote.protocol import BrowseFiles, FilesList, deserialize, serialize
from tests.test_multisession import _mk_ctx, _mk_machine


@pytest.mark.asyncio
async def test_browse_and_recursive_search_are_private_and_session_scoped(tmp_path: Path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src' / 'main.py').write_text('hello')
    (tmp_path / 'README.md').write_text('title')
    (tmp_path / '.env').write_text('secret')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'node_modules' / 'main.js').write_text('dependency')
    machine, transport = _mk_machine()
    machine.sessions['dsh@s-1'] = _mk_ctx('dsh@s-1')
    machine.sessions['dsh@s-1'].cwd = str(tmp_path)
    cmd = BrowseFiles(sid='dsh@s-1', client_id='client-1', request_id='req-files')
    response = await machine._handle_browse_files(cmd)
    assert response.parent is None
    assert response.to == 'client-1'
    assert response.sid == 'dsh@s-1'
    assert '.env' not in [entry.name for entry in response.entries]
    assert response.entries[-1].name == 'README.md'
    assert isinstance(deserialize(serialize(response)), FilesList)
    search = await machine._handle_browse_files(cmd.model_copy(update={'query': 'MAIN'}))
    assert [entry.path for entry in search.entries] == ['src/main.py']
    child = await machine._handle_browse_files(cmd.model_copy(update={'path': 'src'}))
    assert child.parent == '.'
    assert child.entries[0].path == 'src/main.py'
    assert transport.sent[-1] is child


@pytest.mark.asyncio
async def test_browse_refuses_traversal_symlinks_and_foreign_session(tmp_path: Path):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'outside').symlink_to(tmp_path, target_is_directory=True)
    machine, transport = _mk_machine()
    ctx = _mk_ctx('dsh@s-1')
    ctx.cwd = str(root)
    machine.sessions['dsh@s-1'] = ctx
    cmd = BrowseFiles(sid='dsh@s-1', client_id='client-1', request_id='req-files')
    for path in ['..', 'outside']:
        response = await machine._handle_browse_files(cmd.model_copy(update={'path': path}))
        assert response.error
        assert response.entries == []
        assert transport.sent[-1] is response
    missing = await machine._handle_browse_files(cmd.model_copy(update={'sid': 'dsh@missing'}))
    assert missing.error
    assert missing.entries == []
    root_list = await machine._handle_browse_files(cmd)
    assert root_list.entries == []


def test_search_caps_results(tmp_path: Path):
    for i in range(205):
        (tmp_path / f'item-{i}.txt').touch()
    machine, _ = _mk_machine()
    response = machine._browse_workspace_files(str(tmp_path), '.', 'item')
    assert len(response['entries']) == 200
    assert response['truncated']
