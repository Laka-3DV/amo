// Page-lifecycle reconnect recovery.
//
// Field failure this guards against: on a phone the page is suspended while it
// is hidden (screen lock, app switch) and WebKit stops running its timers. On
// return the socket can be wedged in CONNECTING — no open, no error, no close —
// so onclose-driven recovery never runs, and the old heartbeat only acted while
// readyState === OPEN. The UI then sits on "connecting" forever while the relay
// sees zero traffic from the device.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { RelayWs } from "../src/ws.ts";

type Listener = (event: unknown) => void;

class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static readonly instances: FakeWebSocket[] = [];
  static defaultState: number = FakeWebSocket.OPEN;

  readonly sent: string[] = [];
  readyState: number = FakeWebSocket.defaultState;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(raw: string): void { this.sent.push(raw); }
  close(): void { this.closed = true; this.readyState = FakeWebSocket.CLOSED; }
}

const windowListeners = new Map<string, Listener[]>();
const documentListeners = new Map<string, Listener[]>();

Object.assign(globalThis, {
  window: {
    location: { protocol: "https:", host: "dsh.example.test" },
    addEventListener: (type: string, fn: Listener) => {
      windowListeners.set(type, [...(windowListeners.get(type) ?? []), fn]);
    },
    removeEventListener: (type: string, fn: Listener) => {
      windowListeners.set(type,
        (windowListeners.get(type) ?? []).filter((entry) => entry !== fn));
    },
  },
  document: {
    visibilityState: "visible",
    addEventListener: (type: string, fn: Listener) => {
      documentListeners.set(type, [...(documentListeners.get(type) ?? []), fn]);
    },
    removeEventListener: (type: string, fn: Listener) => {
      documentListeners.set(type,
        (documentListeners.get(type) ?? []).filter((entry) => entry !== fn));
    },
  },
  WebSocket: FakeWebSocket,
  sessionStorage: {
    getItem: () => null, setItem: () => {}, removeItem: () => {},
  },
});

interface Internals {
  connect(): void;
  watchdogTick(): void;
  reconnectNow(detail?: string): void;
}

/** Read a private field through a fresh reference so TS narrowing from an
 *  earlier assignment in the same block cannot collapse it to `null`. */
function socketOf(relay: RelayWs): FakeWebSocket | null {
  return (relay as unknown as { ws: FakeWebSocket | null }).ws;
}
function setField(relay: RelayWs, name: string, value: unknown): void {
  (relay as unknown as Record<string, unknown>)[name] = value;
}

function freshRelay(onConnState: (state: string, detail?: string) => void =
  () => {}) {
  FakeWebSocket.instances.length = 0;
  const relay = new RelayWs({ onEvent: () => {}, onConnState });
  return { relay, api: relay as unknown as Internals };
}

const CONNECT_STALL_MS = 12_000;
const SILENT_OPEN_MS = 45_000;

// 1. A handshake frozen in CONNECTING is rebuilt instead of waiting forever.
{
  FakeWebSocket.defaultState = FakeWebSocket.CONNECTING;
  const { relay, api } = freshRelay();
  setField(relay, "ws", null);
  setField(relay, "connectStartedAt", 0);
  api.connect();
  const wedged = socketOf(relay);
  assert.ok(wedged, "connect() must dial");
  assert.equal(wedged.readyState, FakeWebSocket.CONNECTING);

  setField(relay, "connectStartedAt", Date.now() - (CONNECT_STALL_MS - 1000));
  api.watchdogTick();
  assert.equal(socketOf(relay), wedged,
    "a handshake inside the stall window must not be torn down");
  assert.equal(wedged.closed, false);

  setField(relay, "connectStartedAt", Date.now() - (CONNECT_STALL_MS + 1000));
  api.watchdogTick();
  assert.notEqual(socketOf(relay), wedged,
    "a handshake stuck past the stall window must be rebuilt");
  assert.equal(wedged.closed, true,
    "the wedged socket must be closed, not leaked");
  relay.stop();
  FakeWebSocket.defaultState = FakeWebSocket.OPEN;
}

// 2. A silent OPEN socket (half-open TCP, no close frame) is rebuilt, while a
//    live one survives.
{
  const { relay, api } = freshRelay();
  setField(relay, "ws", null);
  setField(relay, "connectStartedAt", 0);
  api.connect();
  const socket = socketOf(relay);
  assert.ok(socket);
  socket.onopen?.();
  setField(relay, "lastRecvAt", Date.now() - 1000);
  api.watchdogTick();
  assert.equal(socketOf(relay), socket, "a live link must survive the watchdog");
  setField(relay, "lastRecvAt", Date.now() - (SILENT_OPEN_MS + 1000));
  api.watchdogTick();
  assert.notEqual(socketOf(relay), socket, "a silent link must be rebuilt");
  relay.stop();
}

// 3. No socket at all with no pending timer: the frozen page's timer is gone,
//    so the watchdog has to dial (or re-arm) by itself.
{
  const { relay, api } = freshRelay();
  setField(relay, "ws", null);
  setField(relay, "reconnectTimer", null);
  setField(relay, "reconnectDueAt", 0);
  setField(relay, "connectStartedAt", Date.now());
  api.watchdogTick();
  assert.ok(socketOf(relay),
    "a missing reconnect timer must be replaced by an immediate dial");
  relay.stop();
}

// 4. pageshow(persisted) recovery: the back/forward cache restore path, where
//    visibilitychange never fires because the page returns already visible.
{
  FakeWebSocket.defaultState = FakeWebSocket.CONNECTING;
  const { relay } = freshRelay();
  relay.start();
  const wedged = socketOf(relay);
  assert.ok(wedged);
  assert.equal(wedged.readyState, FakeWebSocket.CONNECTING);
  const handlers = windowListeners.get("pageshow") ?? [];
  assert.equal(handlers.length, 1,
    "the client must listen for pageshow to catch bfcache restores");
  handlers[0]({ persisted: false });
  assert.equal(socketOf(relay), wedged,
    "a normal (non-restore) pageshow must not disturb the first dial");
  handlers[0]({ persisted: true });
  assert.notEqual(socketOf(relay), wedged,
    "a bfcache restore must re-dial instead of keeping a dead socket");
  assert.equal(wedged.closed, true, "the abandoned socket must be closed");
  relay.stop();
  assert.equal(windowListeners.get("pageshow")?.length, 0,
    "stop() must release the lifecycle listeners");
  assert.equal(windowListeners.get("online")?.length, 0,
    "stop() must release the online listener");
  FakeWebSocket.defaultState = FakeWebSocket.OPEN;
}

// 5. online recovery: a radio hand-off invalidates the socket silently, with no
//    close event for onclose to act on.
{
  assert.equal(windowListeners.get("online")?.length ?? 0, 0,
    "lifecycle listeners are registered by start(), not the constructor");
  FakeWebSocket.defaultState = FakeWebSocket.CONNECTING;
  const { relay } = freshRelay();
  relay.start();
  assert.equal(windowListeners.get("online")?.length, 1,
    "the client must listen for online to recover after a radio switch");
  const wedged = socketOf(relay);
  assert.ok(wedged);
  (windowListeners.get("online") ?? [])[0]({});
  assert.notEqual(socketOf(relay), wedged,
    "coming back online must re-dial a socket that never opened");
  relay.stop();
  FakeWebSocket.defaultState = FakeWebSocket.OPEN;
}

// 6. The manual escape hatch re-dials and reports the retry.
{
  const seen: string[] = [];
  const { relay, api } = freshRelay((state) => seen.push(state));
  relay.start();
  const first = socketOf(relay);
  seen.length = 0;
  api.reconnectNow("正在重新连接…");
  assert.notEqual(socketOf(relay), first, "重连 must dial a fresh socket");
  assert.ok(seen.includes("reconnecting") && seen.includes("connecting"),
    "a manual reconnect must drive the connection state machine");
  relay.stop();
}

// 7. Repeated failed dials are surfaced instead of looking like a slow start,
//    and a successful open clears the warning.
{
  FakeWebSocket.defaultState = FakeWebSocket.CONNECTING;
  const states: Array<[string, string | undefined]> = [];
  const { relay, api } = freshRelay(
    (state, detail) => states.push([state, detail]));
  relay.start();
  for (let attempt = 0; attempt < 3; attempt += 1) {
    setField(relay, "connectStartedAt", Date.now() - (CONNECT_STALL_MS + 1000));
    api.watchdogTick();
  }
  const warned = states.filter(([, detail]) => typeof detail === "string");
  assert.equal(warned.length, 1,
    "three stuck dials must produce one explanation, not a stream of them");
  assert.match(String(warned[0][1]), /连不上中继/);
  assert.equal(warned[0][0], "reconnecting");
  const live = socketOf(relay);
  assert.ok(live);
  live.onopen?.();
  states.length = 0;
  setField(relay, "connectStartedAt", Date.now() - (CONNECT_STALL_MS + 1000));
  api.watchdogTick();
  assert.equal(states.filter(([, detail]) => typeof detail === "string").length, 0,
    "a successful open must reset the stuck-dial warning");
  relay.stop();
  FakeWebSocket.defaultState = FakeWebSocket.OPEN;
}

// 8. Source guards: the watchdog must run in every socket state, and must be
//    armed by start() rather than by onopen (which never fires for a wedged
//    handshake).
{
  const source = readFileSync(resolve(process.cwd(), "src/ws.ts"), "utf8");
  assert.match(source, /this\.startWatchdog\(\);/,
    "start() must arm the watchdog");
  const tick = source.slice(source.indexOf("private watchdogTick"));
  assert.match(tick, /if \(!ws\)/,
    "the watchdog must cover the no-socket state");
  assert.match(tick, /readyState === WebSocket\.CONNECTING/,
    "the watchdog must cover a wedged handshake");
  assert.match(tick, /reconnectTimer === null \|\| overdue/,
    "the watchdog must re-arm a lost reconnect timer");
  assert.match(tick, /readyState === WebSocket\.OPEN && this\.lastRecvAt !== 0/,
    "the watchdog must also cover a silent open link");
  assert.ok(source.indexOf("this.watchdogTimer = setInterval") > 0
    && source.indexOf("private watchdogTick") > 0,
  "the watchdog must be a real interval, not a one-shot check");
}

// A healthy socket needs projection repair on return, not just a ping. The
// paired visibility/pageshow events must not launch duplicate recovery reads.
{
  let reads = 0;
  const relay = new RelayWs({onEvent: () => {}, onConnState: () => {}, onResume: () => { reads++; }});
  relay.start();
  socketOf(relay)?.onopen?.();
  (documentListeners.get("visibilitychange") ?? [])[0]({});
  (windowListeners.get("pageshow") ?? [])[0]({persisted:true});
  assert.equal(reads, 1);
  assert.equal(FakeWebSocket.instances.length > 0, true);
  relay.stop();
}
// Cached cursors are retained for dedupe, but a cold page must not replay
// every cached conversation before choosing its visible session.
{
  const {relay} = freshRelay();
  const cursors = Object.fromEntries(Array.from({length:64}, (_,i)=>[`dsh@cached-${i}`, i+1]));
  relay.seedReplayState(cursors, {});
  relay.start();const socket=socketOf(relay)!;socket.onopen?.();
  const hello=JSON.parse(socket.sent[0]);
  assert.deepEqual(hello.cursors, {});
  assert.equal(relay.lastSeqFor("dsh@cached-63"),64);
  relay.setFocusedSid("dsh@cached-63", "dsh");
  relay.reconnectNow();const resumed=socketOf(relay)!;resumed.onopen?.();
  const recovery=JSON.parse(resumed.sent[0]);
  assert.equal(recovery.cursors["dsh@cached-63"],64);
  relay.stop();
}
console.log("mobile-reconnect tests passed");
