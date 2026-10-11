import type { SessionInfo } from "./protocol";
import { encodeNotificationRoute } from "./notification-route.ts";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Only persisted native threads can address the desktop app. No custom DSH
 * scheme is assumed: its installed CLI does not register a native URL handler. */
export function nativeSessionLink(session: Pick<SessionInfo, "engine" | "session_id" | "native_session_id">): string | null {
  if (session.engine !== "codex") return null;
  const id = session.native_session_id ?? (session.session_id.startsWith("codex@")
    ? session.session_id.slice(6) : "");
  return UUID.test(id) ? `codex://threads/${id}` : null;
}

/** Reuse authorized device/session resolution. A link grants no access and
 * carries no password, prompt or filesystem path. Fragments stay off HTTP. */
export function remoteSessionLink(session: Pick<SessionInfo, "engine" | "session_id" | "space">, machineId: string | undefined, origin: string): string | null {
  if (!machineId || !["dsh", "codex"].includes(session.engine ?? "dsh")
      || (session.space && session.space !== "code")) return null;
  try {
    const base = new URL(origin);
    if (!['https:', 'http:'].includes(base.protocol) || base.username || base.password) return null;
    const path = encodeNotificationRoute({machine_id: machineId, session_id: session.session_id,
      engine: session.engine === "codex" ? "codex" : "dsh", space: "code"});
    return path === "/" ? null : `${base.origin}${path}`;
  } catch { return null; }
}
