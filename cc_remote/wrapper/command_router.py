"""Command-type dispatch for :class:`WrapperMachine`.

The router deliberately owns only the stable ``type -> handler`` decision.
Reliable-command deduplication, scheduling lanes, ownership checks, ACKs and
unexpected-command logging remain lifecycle concerns of ``WrapperMachine``.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

from cc_remote.protocol import Pong


COMMAND_HANDLER_NAMES = MappingProxyType({
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
})

WORK_MUTATION_COMMANDS = frozenset()

UNHANDLED_COMMAND = object()


class CommandRouter:
    """Resolve a parsed command to the machine's existing async handler."""

    def __init__(self, target: Any):
        self._target = target

    async def dispatch(self, command: Any) -> Any:
        command_type = command.type
        if command_type == "hello":
            if getattr(command, "role", None) != "client":
                return UNHANDLED_COMMAND
            # Client hellos are bound at the relay; the wrapper has nothing to
            # do for them unless a machine defines an explicit handler.
            handler = getattr(self._target, "_handle_client_hello", None)
            if handler is None:
                return None
            return await handler(command)

        if command_type == "ping":
            await self._target._emit_focused(Pong(n=command.n))
            return None

        if command_type in WORK_MUTATION_COMMANDS:
            return await self._target._handle_work_mutation(command)

        handler_name = COMMAND_HANDLER_NAMES.get(command_type)
        if handler_name is None:
            return UNHANDLED_COMMAND
        # Resolve for every dispatch. Tests and embedded consumers intentionally
        # replace selected handlers on a live WrapperMachine instance.
        handler = getattr(self._target, handler_name)
        return await handler(command)
