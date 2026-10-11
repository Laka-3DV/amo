import type { SessionInfo } from "./protocol";

export interface CreatedSessionFocus {
  scopeKey: string;
  session: SessionInfo;
  expiresAt: number;
}

/** The native catalog can lag a successful create/rekey. An absent row during
 * that window is not deletion and must not send the user to another chat. */
export function retainCreatedSession(
  lease: CreatedSessionFocus | null, scopeKey: string,
  sessions: SessionInfo[], now = Date.now(),
): { sessions: SessionInfo[]; lease: CreatedSessionFocus | null } {
  if (!lease || lease.scopeKey !== scopeKey) return { sessions, lease };
  if (sessions.some(row => row.session_id === lease.session.session_id)
      || now >= lease.expiresAt) return { sessions, lease: null };
  return { sessions: [lease.session, ...sessions], lease };
}
