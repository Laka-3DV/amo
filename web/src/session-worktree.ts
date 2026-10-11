import type { Engine, SessionInfo } from "./protocol";

export interface SessionMenuCapabilities {
  rename: boolean;
  archive: boolean;
  delete: boolean;
}

export interface PendingSessionFork {
  requestId: string;
  parentSessionId: string;
  forkPointId: string;
  engine: Engine;
}

export interface ForkFocusLease {
  requestId: string;
  parentSessionId: string;
  childSessionId: string;
  engine: Engine;
  space: "code";
  machineId: string;
  cwd: string;
  gitBranch?: string | null;
  refreshAt: number;
}

export const FORK_FOCUS_REFRESH_MS = 15_000;

export function forkFocusLeaseSession(
  lease: ForkFocusLease | null,
  sessions: readonly SessionInfo[],
  machineId: string,
  engine: Engine,
  space: "code" | "work",
): SessionInfo | null {
  if (!lease || lease.machineId !== machineId || lease.engine !== engine
      || lease.space !== space
      || sessions.some((session) => (
        session.session_id === lease.childSessionId
      ))) return null;
  return {
    session_id: lease.childSessionId,
    summary: "派生会话",
    cwd: lease.cwd,
    git_branch: lease.gitBranch,
    engine: lease.engine,
    space: lease.space,
    forked_from_id: lease.parentSessionId,
    state: "idle",
    last_modified: String(lease.refreshAt - FORK_FOCUS_REFRESH_MS),
    provisional_fork: true,
  };
}

export function withoutForkFocusPlaceholder(
  sessions: readonly SessionInfo[],
  lease: ForkFocusLease | null,
): SessionInfo[] {
  if (!lease) return [...sessions];
  return sessions.filter((session) => !(
    session.provisional_fork
    &&
    session.session_id === lease.childSessionId
    && session.forked_from_id === lease.parentSessionId
    && session.engine === lease.engine
    && (session.space ?? "code") === lease.space
  ));
}

export function sessionMenuCapabilities(session: SessionInfo): SessionMenuCapabilities {
  return {
    rename: true,
    archive: session.engine !== "dsh" || session.tag !== "archived",
    delete: session.engine !== "dsh",
  };
}

export function matchesSessionForkRequest(
  pending: PendingSessionFork | null,
  requestId: string | null | undefined,
  parentSessionId?: string | null,
  forkPointId?: string | null,
): boolean {
  return pending !== null
    && requestId === pending.requestId
    && (parentSessionId == null || parentSessionId === pending.parentSessionId)
    && (forkPointId == null || forkPointId === pending.forkPointId);
}

export function canForkTurn<T extends { done: boolean; forkPointId?: string }>(
  _engine: Engine,
  turn: T,
): turn is T & { done: true; forkPointId: string } {
  return turn.done && !!turn.forkPointId;
}
