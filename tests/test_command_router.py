"""Zero-token characterization tests for wrapper command dispatch."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cc_remote.protocol import Error, Pong
from cc_remote.wrapper.command_router import (
    COMMAND_HANDLER_NAMES,
    UNHANDLED_COMMAND,
    WORK_MUTATION_COMMANDS,
    CommandRouter,
)
from tests.test_multisession import _mk_machine


EXPECTED_COMMAND_HANDLERS = {
    "query": "_handle_query",
    "cancel_queued_query": "_handle_cancel_queued_query",
    "get_queued_query": "_handle_get_queued_query",
    "update_queued_query": "_handle_update_queued_query",
    "steer": "_handle_steer",
    "interrupt": "_handle_interrupt",
    "set_model": "_handle_set_model",
    "set_effort": "_handle_set_effort",
    "set_perm": "_handle_set_perm",
    "get_context": "_handle_get_context",
    "get_account_usage": "_handle_get_account_usage",
    "get_history": "_handle_get_history",
    "get_turn_detail": "_handle_get_turn_detail",
    "get_history_image": "_handle_get_history_image",
    "browse_files": "_handle_browse_files",
    "get_file_preview": "_handle_get_file_preview",
    "get_diff": "_handle_get_diff",
    "get_models": "_handle_get_models",
    "get_engine_capabilities": "_handle_get_engine_capabilities",
    "answer_question": "_handle_answer_question",
    "list_sessions": "_handle_list_sessions",
    "switch_session": "_handle_switch_session",
    "new_session": "_handle_new_session",
    "rename_session": "_handle_rename_session",
    "archive_session": "_handle_archive_session",
    "pin_session": "_handle_pin_session",
    "delete_session": "_handle_delete_session",
    "fork_session": "_handle_fork_session",
    "acknowledge_completion": "_handle_acknowledge_completion",
}

EXPECTED_WORK_MUTATIONS: set[str] = set()


class _Target:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.emitted: list[object] = []

    def __getattr__(self, name: str):
        if not name.startswith("_handle_"):
            raise AttributeError(name)

        async def handle(command):
            self.calls.append((name, command.type))
            return f"result:{command.type}"

        return handle

    async def _emit_focused(self, event) -> None:
        self.emitted.append(event)


def test_command_router_has_the_characterized_command_surface():
    assert dict(COMMAND_HANDLER_NAMES) == EXPECTED_COMMAND_HANDLERS
    assert set(WORK_MUTATION_COMMANDS) == EXPECTED_WORK_MUTATIONS
    assert not (set(COMMAND_HANDLER_NAMES) & set(WORK_MUTATION_COMMANDS))


@pytest.mark.parametrize(
    ("command_type", "handler_name"),
    sorted(EXPECTED_COMMAND_HANDLERS.items()),
)
def test_command_router_resolves_direct_handlers_at_dispatch_time(
        command_type, handler_name):
    async def run():
        target = _Target()
        router = CommandRouter(target)
        command = SimpleNamespace(type=command_type)

        result = await router.dispatch(command)

        assert result == f"result:{command_type}"
        assert target.calls == [(handler_name, command_type)]

    asyncio.run(run())


@pytest.mark.parametrize("command_type", sorted(EXPECTED_WORK_MUTATIONS))
def test_command_router_groups_work_mutations_without_changing_the_command(
        command_type):
    async def run():
        target = _Target()
        router = CommandRouter(target)
        command = SimpleNamespace(type=command_type)

        result = await router.dispatch(command)

        assert result == f"result:{command_type}"
        assert target.calls == [("_handle_work_mutation", command_type)]

    asyncio.run(run())


def test_command_router_preserves_client_hello_and_ping_special_cases():
    async def run():
        target = _Target()
        router = CommandRouter(target)

        hello_result = await router.dispatch(SimpleNamespace(
            type="hello", role="client"))
        non_client_result = await router.dispatch(SimpleNamespace(
            type="hello", role="wrapper"))
        ping_result = await router.dispatch(SimpleNamespace(type="ping", n=7))

        assert hello_result == "result:hello"
        assert target.calls == [("_handle_client_hello", "hello")]
        assert non_client_result is UNHANDLED_COMMAND
        assert ping_result is None
        assert len(target.emitted) == 1
        assert isinstance(target.emitted[0], Pong)
        assert target.emitted[0].n == 7

    asyncio.run(run())


def test_command_router_returns_sentinel_for_unknown_command():
    async def run():
        target = _Target()
        router = CommandRouter(target)

        result = await router.dispatch(SimpleNamespace(type="future_command"))

        assert result is UNHANDLED_COMMAND
        assert target.calls == []
        assert target.emitted == []

    asyncio.run(run())
