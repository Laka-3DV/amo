import type {
  Engine,
  SessionInfo,
  SessionList,
  Space,
  State,
} from "./protocol";

/** Apply one authoritative/partial-success catalog response exactly once.
 * Every consumer (React state, surface caches, RelayWs ownership maps) must use
 * this same projection. */
export function normalizeSessionList(
  event: SessionList,
): SessionInfo[] {
  return [...event.sessions];
}

export function shouldAcceptSessionList(
  activeEngine: Engine,
  activeSpace: Space,
  event: SessionList,
): boolean {
  return event.engine === activeEngine && (event.space ?? "code") === activeSpace;
}

export function updateScopedSessionLifecycle(
  catalog: Readonly<Record<string, SessionInfo[]>>,
  engine: Engine,
  space: Space,
  sid: string,
  state: State,
): Record<string, SessionInfo[]> {
  const key = `${space}:${engine}`;
  const listed = catalog[key];
  if (!listed) return catalog as Record<string, SessionInfo[]>;
  let changed = false;
  const updated = listed.map((session) => {
    if (session.session_id !== sid || session.state === state) return session;
    changed = true;
    return { ...session, state };
  });
  return changed ? { ...catalog, [key]: updated }
    : catalog as Record<string, SessionInfo[]>;
}

/** One sidebar across both agents; session IDs remain engine-namespaced. */
export function mergedAgentSessions(catalog: Readonly<Record<string, SessionInfo[]>>): SessionInfo[] {
  const rows = new Map<string, SessionInfo>();
  for (const key of ["code:dsh", "code:codex"]) {
    for (const session of catalog[key] ?? []) rows.set(session.session_id, session);
  }
  return [...rows.values()];
}
