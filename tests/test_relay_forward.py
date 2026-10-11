import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest

from cc_remote.protocol import (
    PROTOCOL_VERSION,
    Delta, Hello, ListSessions, Query, TurnEnd, TurnNotificationContext,
    TurnResult, serialize,
)
from cc_remote.relay.forward import ClientConn, SlowClientError
from cc_remote.relay import pairing
from cc_remote.relay.pairing import RelayHub


class FakeWs:
    def __init__(self):
        self.sent = []
        self.closed = []
        self.block = asyncio.Event()
        self.incoming = asyncio.Queue()

    async def receive_text(self):
        return await self.incoming.get()

    async def send_text(self, raw):
        await self.block.wait()
        self.sent.append(raw)

    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))


class ScriptedWs(FakeWs):
    def __init__(self):
        super().__init__()
        self.block.set()


def test_client_queue_is_bounded_by_items_and_bytes():
    async def run():
        ws = FakeWs()
        conn = ClientConn(ws, cap=1, client_id="c", byte_cap=4096)
        conn.start()
        await conn.send(Delta(message_id="m", text="first"))
        # Let the sender take the first item and block in send_text.
        await asyncio.sleep(0)
        await conn.send(Delta(message_id="m", text="second"))
        with pytest.raises(SlowClientError):
            await conn.send(TurnEnd(result=TurnResult(
                subtype="success", duration_ms=1, is_error=False)))
        assert conn.queue.qsize() == 1
        assert conn.queued_bytes <= conn.byte_cap
        await conn.stop(code=4008, reason="slow client")
        assert ws.closed == [(4008, "slow client")]

    asyncio.run(run())


def test_wrapper_protocol_mismatch_is_closed_and_releases_single_slot():
    async def run():
        hub = RelayHub(SimpleNamespace())
        ws = ScriptedWs()
        await ws.incoming.put(json.dumps({
            "v": 4, "type": "hello", "role": "wrapper", "ts": 1,
        }))

        await hub.serve_wrapper(ws)

        assert ws.closed == [(
            pairing.PROTOCOL_MISMATCH_CLOSE_CODE,
            pairing.PROTOCOL_MISMATCH_CLOSE_REASON,
        )]
        assert hub.wrapper_connected is False
        assert json.loads(ws.sent[0])["code"] == "protocol"

    asyncio.run(run())


def test_replacing_client_closes_old_generation():
    async def run():
        cfg = SimpleNamespace(client_queue_cap=4, client_queue_bytes=4096)
        hub = RelayHub(cfg)
        old_ws, new_ws = FakeWs(), FakeWs()
        old = ClientConn(old_ws, 4, "same", 4096)
        new = ClientConn(new_ws, 4, "same", 4096)
        old.start()
        new.start()
        hub._clients["same"] = old
        async with hub._lock:
            previous = hub._clients.get("same")
            hub._clients["same"] = new
        await previous.stop(code=4009, reason="replaced by reconnect")
        assert hub._clients["same"] is new
        assert old.closed
        assert old_ws.closed == [(4009, "replaced by reconnect")]
        await new.stop()

    asyncio.run(run())


def test_connection_routed_replay_never_crosses_client_replacement():
    async def run():
        cfg = SimpleNamespace(client_queue_cap=4, client_queue_bytes=4096)
        hub = RelayHub(cfg)
        ws = ScriptedWs()
        current = ClientConn(ws, 4, "same", 4096)
        current.start()
        hub._clients["same"] = current

        await hub._on_wrapper_msg(Delta(
            message_id="stale", text="old", to="same", route_id="old-route"))
        await asyncio.sleep(0)
        assert ws.sent == []

        await hub._on_wrapper_msg(Delta(
            message_id="fresh", text="new", to="same",
            route_id=current.route_id))
        await asyncio.sleep(0)
        assert json.loads(ws.sent[-1])["message_id"] == "fresh"
        await current.stop()

    asyncio.run(run())


def test_only_live_turn_end_triggers_relay_event_hook():
    async def run():
        notified: list[tuple[str, object]] = []
        ready = asyncio.Event()

        async def on_turn_end(machine_id, msg):
            notified.append((machine_id, msg))
            ready.set()

        hub = RelayHub(SimpleNamespace(), on_live_turn_end=on_turn_end)
        result = TurnResult(subtype="success", duration_ms=1, is_error=False)
        context = TurnNotificationContext(engine="codex", space="code")
        await hub._on_wrapper_msg(TurnEnd(
            result=result,
            notification_context=context,
            to="replay-client",
        ), "nono")
        await asyncio.sleep(0)
        assert notified == [], "a client replay must not emit a second push"

        await hub._on_wrapper_msg(TurnEnd(result=result), "nono")
        await asyncio.sleep(0)
        assert notified == [], "a context-free buffered terminal must not emit push"

        await hub._on_wrapper_msg(TurnEnd(
            result=result,
            notification_context=context,
        ), "nono")
        await asyncio.wait_for(ready.wait(), timeout=1)
        assert notified[0][0] == "nono"

    asyncio.run(run())


def test_relay_overwrites_untrusted_client_hello_route_id():
    class CapturingWrapper:
        def __init__(self):
            self.message = None
            self.received = asyncio.Event()

        async def send_text(self, raw):
            self.message = json.loads(raw)
            self.received.set()

    async def run():
        cfg = SimpleNamespace(
            client_queue_cap=4,
            client_queue_bytes=4096,
            max_clients=2,
            client_hello_timeout=1,
        )
        hub = RelayHub(cfg)
        wrapper = CapturingWrapper()
        hub._wrapper_ws = wrapper
        ws = ScriptedWs()
        await ws.incoming.put(serialize(Hello(
            role="client", client_id="same", route_id="attacker-chosen")))
        task = asyncio.create_task(hub.serve_client(ws))
        await asyncio.wait_for(wrapper.received.wait(), 1)

        assert wrapper.message["route_id"] != "attacker-chosen"
        assert wrapper.message["route_id"] == hub._clients["same"].route_id
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_client_connection_limit_has_one_bounded_reconnect_probe_slot():
    async def run():
        cfg = SimpleNamespace(
            client_queue_cap=4,
            client_queue_bytes=4096,
            max_clients=1,
            client_hello_timeout=1,
        )
        hub = RelayHub(cfg)
        waiting = ScriptedWs()
        first = asyncio.create_task(hub.serve_client(waiting))
        await asyncio.sleep(0)
        assert hub.client_count == 1

        probe = ScriptedWs()
        probe_task = asyncio.create_task(hub.serve_client(probe))
        await asyncio.sleep(0)
        assert hub.client_count == 2

        rejected = ScriptedWs()
        await hub.serve_client(rejected)
        assert rejected.closed == [(
            pairing.CLIENT_LIMIT_CLOSE_CODE,
            pairing.CLIENT_LIMIT_CLOSE_REASON,
        )]
        assert hub.client_count == 2

        first.cancel()
        probe_task.cancel()
        await asyncio.gather(first, probe_task, return_exceptions=True)
        assert hub.client_count == 0

    asyncio.run(run())


def test_client_must_send_hello_before_absolute_timeout():
    async def run():
        cfg = SimpleNamespace(
            client_queue_cap=4,
            client_queue_bytes=4096,
            max_clients=2,
            client_hello_timeout=0.01,
        )
        hub = RelayHub(cfg)
        silent = ScriptedWs()
        await hub.serve_client(silent)
        assert silent.closed == [(
            pairing.CLIENT_HELLO_TIMEOUT_CLOSE_CODE,
            pairing.CLIENT_HELLO_TIMEOUT_CLOSE_REASON,
        )]
        assert hub.client_count == 0

    asyncio.run(run())


def test_client_cannot_originate_wrapper_only_frames():
    class Wrapper:
        async def send_text(self, _raw):
            return None

    async def run():
        cfg = SimpleNamespace(
            client_queue_cap=4,
            client_queue_bytes=4096,
            max_clients=2,
            client_hello_timeout=1,
        )
        hub = RelayHub(cfg)
        hub._wrapper_ws = Wrapper()
        ws = ScriptedWs()
        await ws.incoming.put(serialize(Hello(role="client", client_id="client-1")))
        await ws.incoming.put(serialize(Delta(message_id="server-only", text="x")))

        await hub.serve_client(ws)

        assert ws.closed == [(
            pairing.PROTOCOL_ERROR_CLOSE_CODE,
            pairing.PROTOCOL_ERROR_CLOSE_REASON,
        )]
        assert hub.client_count == 0

    asyncio.run(run())


def test_replacement_is_linearized_after_old_inflight_wrapper_send():
    class BlockingWrapper:
        def __init__(self):
            self.completed = []
            self.old_query_entered = asyncio.Event()
            self.release_old_query = asyncio.Event()
            self.new_hello_completed = asyncio.Event()

        async def send_text(self, raw):
            msg = json.loads(raw)
            marker = (msg["type"], msg.get("msg_id"))
            if marker == ("query", "old-command"):
                self.old_query_entered.set()
                await self.release_old_query.wait()
            self.completed.append(marker)
            if self.completed.count(("hello", None)) == 2:
                self.new_hello_completed.set()

    async def run():
        cfg = SimpleNamespace(
            client_queue_cap=4,
            client_queue_bytes=4096,
            max_clients=4,
            client_hello_timeout=1,
        )
        hub = RelayHub(cfg)
        wrapper = BlockingWrapper()
        hub._wrapper_ws = wrapper
        old_ws, new_ws = ScriptedWs(), ScriptedWs()
        await old_ws.incoming.put(serialize(Hello(role="client", client_id="same")))
        await old_ws.incoming.put(serialize(Query(prompt="old", msg_id="old-command")))
        old_task = asyncio.create_task(hub.serve_client(old_ws))
        await wrapper.old_query_entered.wait()

        await new_ws.incoming.put(serialize(Hello(role="client", client_id="same")))
        new_task = asyncio.create_task(hub.serve_client(new_ws))
        await asyncio.sleep(0)
        # Replacement waits behind the old send's linearization lock.
        assert hub._clients["same"].ws is old_ws

        wrapper.release_old_query.set()
        await asyncio.wait_for(wrapper.new_hello_completed.wait(), 1)
        assert hub._clients["same"].ws is new_ws
        assert wrapper.completed == [
            ("hello", None),
            ("query", "old-command"),
            ("hello", None),
        ]
        assert old_ws.closed == [(4009, "replaced by reconnect")]

        for task in (old_task, new_task):
            task.cancel()
        await asyncio.gather(old_task, new_task, return_exceptions=True)

    asyncio.run(run())


def test_named_wrappers_route_events_only_to_their_machine_clients():
    async def run():
        cfg = SimpleNamespace(client_queue_cap=4, client_queue_bytes=4096)
        hub = RelayHub(cfg)
        wrapper_a, wrapper_b = ScriptedWs(), ScriptedWs()
        await wrapper_a.incoming.put(serialize(Hello(
            role="wrapper", machine_id="mac", cc_session_id="mac-session")))
        await wrapper_b.incoming.put(serialize(Hello(
            role="wrapper", machine_id="nono", cc_session_id="nono-session")))
        task_a = asyncio.create_task(hub.serve_wrapper(wrapper_a))
        task_b = asyncio.create_task(hub.serve_wrapper(wrapper_b))
        for _ in range(20):
            if hub.machine_ids == ["mac", "nono"]:
                break
            await asyncio.sleep(0)
        assert hub.machine_ids == ["mac", "nono"]

        client_a_ws, client_b_ws = ScriptedWs(), ScriptedWs()
        client_a = ClientConn(client_a_ws, 4, "phone-a", 4096)
        client_b = ClientConn(client_b_ws, 4, "phone-b", 4096)
        client_a.start()
        client_b.start()
        hub._ensure_clients_for("mac")["phone-a"] = client_a
        hub._ensure_clients_for("nono")["phone-b"] = client_b

        await hub._on_wrapper_msg(
            Delta(message_id="mac-only", text="a"), "mac")
        await asyncio.sleep(0)
        assert json.loads(client_a_ws.sent[-1])["message_id"] == "mac-only"
        assert client_b_ws.sent == []

        await hub._on_wrapper_msg(
            Delta(message_id="nono-only", text="b"), "nono")
        await asyncio.sleep(0)
        assert json.loads(client_b_ws.sent[-1])["message_id"] == "nono-only"
        assert all(json.loads(raw).get("message_id") != "nono-only"
                   for raw in client_a_ws.sent)

        for task in (task_a, task_b):
            task.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
        await client_a.stop()
        await client_b.stop()

    asyncio.run(run())


def test_machine_bound_wrapper_credential_rejects_other_machine_hello():
    async def run():
        hub = RelayHub(SimpleNamespace())
        ws = ScriptedWs()
        await ws.incoming.put(serialize(Hello(
            role="wrapper", machine_id="mac", cc_session_id="session")))
        await hub.serve_wrapper(ws, expected_machine_id="nono")
        assert ws.closed == [(1008, "machine not authorized")]
        assert hub.machine_ids == []
        assert json.loads(ws.sent[-1])["code"] == "protocol"

    asyncio.run(run())


def test_wildcard_wrapper_cannot_exceed_named_wrapper_limit():
    async def run():
        hub = RelayHub(SimpleNamespace())
        hub._wrappers.update({
            f"machine-{index}": object()
            for index in range(pairing.MAX_NAMED_WRAPPERS)
        })
        ws = ScriptedWs()
        await ws.incoming.put(serialize(Hello(
            role="wrapper",
            machine_id="overflow",
            cc_session_id="session",
        )))

        await hub.serve_wrapper(ws)

        assert len(hub._wrappers) == pairing.MAX_NAMED_WRAPPERS
        assert "overflow" not in hub._wrappers
        assert ws.closed == [(
            pairing.WRAPPER_LIMIT_CLOSE_CODE,
            pairing.WRAPPER_LIMIT_CLOSE_REASON,
        )]
        assert json.loads(ws.sent[-1])["code"] == "busy"

    asyncio.run(run())


def test_named_machine_read_paths_do_not_create_empty_client_buckets():
    async def run():
        hub = RelayHub(SimpleNamespace())

        await hub._on_wrapper_msg(
            Delta(message_id="missing", text="ignored", to="phone"),
            "unused-machine",
        )
        await hub._broadcast(
            Delta(message_id="broadcast", text="ignored"),
            "unused-machine",
        )
        await hub._wrapper_gone("unused-machine")

        assert hub._machine_clients == {}

    asyncio.run(run())


def test_last_named_client_disconnect_prunes_machine_bucket():
    async def run():
        cfg = SimpleNamespace(
            client_queue_cap=4,
            client_queue_bytes=4096,
            max_clients=2,
            client_hello_timeout=1,
        )
        hub = RelayHub(cfg)
        ws = ScriptedWs()
        await ws.incoming.put(serialize(Hello(
            role="client", client_id="phone")))
        task = asyncio.create_task(hub.serve_client(ws, "ephemeral-machine"))
        for _ in range(20):
            if "ephemeral-machine" in hub._machine_clients:
                break
            await asyncio.sleep(0)

        assert list(hub._machine_clients["ephemeral-machine"]) == ["phone"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert "ephemeral-machine" not in hub._machine_clients

    asyncio.run(run())


def _hub_with_wrapper():
    class Wrapper:
        def __init__(self):
            self.forwarded: list[dict] = []

        async def send_text(self, raw):
            self.forwarded.append(json.loads(raw))

    cfg = SimpleNamespace(
        client_queue_cap=64,
        client_queue_bytes=1 << 20,
        max_clients=2,
        client_hello_timeout=1,
    )
    hub = RelayHub(cfg)
    wrapper = Wrapper()
    hub._wrapper_ws = wrapper
    return hub, wrapper


def test_malformed_known_command_is_answered_without_dropping_the_client():
    """One bad frame must not become a reconnect storm.

    Field incident: a version-skewed answer_question was rejected here, the
    socket was closed, the browser replayed the same command from its outbox on
    every reconnect, and the device could never stay connected.
    """
    async def run():
        hub, wrapper = _hub_with_wrapper()
        ws = ScriptedWs()
        await ws.incoming.put(serialize(Hello(role="client", client_id="client-1")))
        # A known type with an invalid field: `answer` may not be an empty string.
        await ws.incoming.put(json.dumps({
            "v": PROTOCOL_VERSION, "type": "answer_question", "ask_id": "a1",
            "answer": "", "outcome": "skip", "cmd_id": "cmd-1", "sid": "s1",
        }))
        await ws.incoming.put(serialize(ListSessions(engine="dsh")))

        # The connection deliberately stays open, so drive the loop and then
        # stop it: a closing client would make the assertion meaningless.
        task = asyncio.create_task(hub.serve_client(ws))
        for _ in range(200):
            if wrapper.forwarded:
                break
            await asyncio.sleep(0)
        # The client sender task flushes on its own tick.
        for _ in range(50):
            await asyncio.sleep(0)
        errors = [
            json.loads(raw) for raw in ws.sent
            if '"error"' in raw
        ]
        closed_before = list(ws.closed)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert closed_before == [], "the client must stay connected"
        assert wrapper.forwarded[-1]["type"] == "list_sessions", (
            "later commands still flow over the same socket"
        )
        assert errors, "the refused command must be answered"
        assert errors[0]["code"] == "protocol"
        assert errors[0]["request_id"] == "cmd-1"
        assert "answer_question" not in [
            frame.get("type") for frame in wrapper.forwarded
        ], "the malformed command must not reach the wrapper"

    asyncio.run(run())


def test_unparseable_or_unknown_frames_still_close_the_socket():
    """A frame we cannot even identify is an attacker, not a version skew."""
    async def run():
        hub, _wrapper = _hub_with_wrapper()
        ws = ScriptedWs()
        await ws.incoming.put(serialize(Hello(role="client", client_id="client-1")))
        await ws.incoming.put(json.dumps({
            "v": PROTOCOL_VERSION, "type": "definitely-not-a-real-type",
        }))

        await hub.serve_client(ws)

        assert ws.closed == [(
            pairing.PROTOCOL_ERROR_CLOSE_CODE,
            pairing.PROTOCOL_ERROR_CLOSE_REASON,
        )]

    asyncio.run(run())
