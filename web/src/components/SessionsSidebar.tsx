import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState, type TouchEvent } from "react";
import type { Engine, SessionInfo, Space, State } from "../protocol";
import type { CompletionBadgeKind } from "../completion-badges";
import { Icon, BrandMark } from "../icons";
import {
  compareSessionsByActivity,
  orderCodeDirectoryGroups,
  visibleDirectorySessions,
} from "../session-order";
import {
  sessionMenuCapabilities,
} from "../session-worktree";
import { useDeviceLayout } from "../use-device-layout";
import { AgentBadge } from "./AgentBadge";
import { activityTimestamp, relativeActivityAt } from "../session-relative-time";
import { RenameSessionDialog } from "./RenameSessionDialog";
import { nativeSessionLink, remoteSessionLink } from "../session-links";
import { SidebarWindow, type SidebarWindowRow } from "./SidebarWindow";

interface Props {
  open: boolean;
  side?: "left" | "right";
  engine: Engine;
  machineId?: string;
  space: Space;
  onSpaceChange: (space: Space) => void;
  sessions: SessionInfo[];
  // Live state per resident session (from the client's runtimes), overriding the
  // list_sessions snapshot so a background session's dot updates in real time.
  liveStates?: Record<string, State>;
  completionBadges?: Record<string, CompletionBadgeKind>;
  activeSessionId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onNewInDir: (cwd: string) => void;
  onClose: () => void;
  onRename: (id: string, title: string) => void;
  onArchive: (id: string, archived: boolean) => void;
  onPin: (session: SessionInfo, pinned: boolean) => void;
  onDelete: (id: string) => void;
}

function basename(cwd?: string | null): string {
  if (!cwd) return "其他";
  const parts = cwd.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || cwd;
}

function sameRecord(a: object | undefined, b: object | undefined): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  const left = a as Record<string, unknown>, right = b as Record<string, unknown>;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every(key => left[key] === right[key]);
}

// App receives every streamed token. Fresh handler/map objects must not make a
// scrolling drawer rerender; handlers still see the latest committed App state.
export function SessionsSidebar(props: Props) {
  const latest = useRef(props);
  useLayoutEffect(() => { latest.current = props; });
  const [actions] = useState(() => ({
    onSpaceChange: (space: Space) => latest.current.onSpaceChange(space),
    onSelect: (id: string) => latest.current.onSelect(id),
    onNew: () => latest.current.onNew(),
    onNewInDir: (cwd: string) => latest.current.onNewInDir(cwd),
    onClose: () => latest.current.onClose(),
    onRename: (id: string, title: string) => latest.current.onRename(id, title),
    onArchive: (id: string, archived: boolean) => latest.current.onArchive(id, archived),
    onPin: (session: SessionInfo, pinned: boolean) => latest.current.onPin(session, pinned),
    onDelete: (id: string) => latest.current.onDelete(id),
  }));
  return <MemoSidebar {...props} {...actions} />;
}
const MemoSidebar = memo(SidebarContent, (a, b) => {
  if (a.machineId !== b.machineId || a.open !== b.open || a.side !== b.side || a.engine !== b.engine || a.space !== b.space) return false;
  // Prepare newly arrived catalogs behind the closed panel. Opening a drawer
  // should reveal its cached rows, not construct hundreds during the gesture.
  if (!b.open) return a.sessions.length === b.sessions.length;
  return a.activeSessionId === b.activeSessionId
    && (a.sessions === b.sessions || a.sessions.length === b.sessions.length
      && a.sessions.every((session, index) => sameRecord(session, b.sessions[index])))
    && sameRecord(a.liveStates, b.liveStates)
    && sameRecord(a.completionBadges, b.completionBadges);
});

function SidebarContent({ open, engine, space, side, machineId,
  sessions, liveStates,
  completionBadges, activeSessionId, onSelect, onNew, onNewInDir, onClose,
  onRename, onArchive, onPin, onDelete }: Props) {
  const [agentFilter,setAgentFilter] = useState<Record<Engine,boolean>>({ dsh:true, codex:true, claude:true });
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!open) return;
    const refresh = () => { if (!document.hidden) setNow(Date.now()); };
    refresh();
    const timer = window.setInterval(refresh, 60_000);
    document.addEventListener("visibilitychange", refresh);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", refresh); };
  }, [open]);
  const activityDates = useMemo(() => {
    const dates = new Map<number, { iso: string; title: string }>();
    for (const session of sessions) {
      const stamp = activityTimestamp(session.last_modified);
      if (stamp === null || dates.has(stamp)) continue;
      const date = new Date(stamp);
      dates.set(stamp, { iso: date.toISOString(), title: `最近活跃：${date.toLocaleString("zh-CN")}` });
    }
    return dates;
  }, [sessions]);
  const activityByDirectory = useMemo(() => {
    const latest = new Map<string, number>();
    for (const session of sessions) {
      if (session.tag === "archived" || !agentFilter[session.engine ?? "dsh"]) continue;
      const stamp = activityTimestamp(session.last_modified);
      const key = session.cwd || "";
      if (stamp !== null && stamp > (latest.get(key) ?? -Infinity)) latest.set(key, stamp);
    }
    return latest;
  }, [sessions, agentFilter]);
  const activityLabel = (value: string | number | null | undefined, group = false) => {
    const timestamp = typeof value === "number" ? value : activityTimestamp(value);
    const text = relativeActivityAt(timestamp, now);
    return <time className={group ? "sgroup-activity" : "scard-activity"}
      dateTime={timestamp === null ? undefined : activityDates.get(timestamp)?.iso}
      title={timestamp === null ? "未提供最近活跃时间" : activityDates.get(timestamp)?.title}
      aria-label={`最近活跃：${text}`}>{text}</time>;
  };
  const [q, setQ] = useState("");
  const [menuCardId, setMenuCardId] = useState<string | null>(null);
  const [lifting, setLifting] = useState(false);
  const [renaming, setRenaming] = useState<{ id: string; value: string } | null>(null);
  const selectTimer = useRef<number | null>(null);
  const pendingSelect = useRef<string | null>(null);
  const cancelSelect = () => {
    if (selectTimer.current !== null) window.clearTimeout(selectTimer.current);
    selectTimer.current = null; pendingSelect.current = null;
  };
  const [copiedId, setCopiedId] = useState<string | null>(null);
  // "archived" group starts collapsed; project groups start expanded.
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({ archived: true });
  const [expandedDirectories, setExpandedDirectories] = useState<Record<string, boolean>>({});
  const [listView, setListView] = useState<"activity" | "directory">("activity");
  const [expandedTitles, setExpandedTitles] = useState<Record<string, boolean>>({});
  const pressTimer = useRef<number | null>(null);
  const pressStart = useRef<{ x: number; y: number } | null>(null);
  const suppressClickUntil = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const scrolling = useRef(false);
  const scrollIdleTimer = useRef<number | null>(null);
  const layout = useDeviceLayout();
  const mobile = layout.overlay;
  const drawerSide = side ?? layout.sidebarSide;
  const agentTypes = ["dsh", "codex"] as const;
  const bottomActivity=true;

  // An activity update can move an entire directory while the user is reading
  // or touching its rows. Keep positions for this browsing visit; badges/titles
  // still use fresh data and the next opening restores recent-activity sorting.
  const scope = `${engine}:${space}:${listView}:${mobile}:${agentTypes.filter(kind => agentFilter[kind]).join(",")}`;
  const compareActivity = (a: SessionInfo, b: SessionInfo) => {
    const running = (s: SessionInfo) => ["running", "interrupting"].includes(liveStates?.[s.session_id] ?? s.state ?? "idle");
    return Number(running(b)) - Number(running(a))
      || Number(!!b.pinned) - Number(!!a.pinned) || compareSessionsByActivity(a,b);
  };
  const activityOrder = [...sessions].sort(compareActivity);
  const allGroups: Record<string, SessionInfo[]> = {};
  for (const s of activityOrder) {
    if (!s.pinned && s.tag !== "archived") {
      (allGroups[s.cwd || ""] ??= []).push(s);
    }
  }
  const captureOrder = () => ({
    open, scope,
    revealSessionId: null as string | null,
    sessionIds: activityOrder.map((s) => s.session_id),
    directories: orderCodeDirectoryGroups(allGroups),
  });
  const [browseOrder, setBrowseOrder] = useState(captureOrder);
  let order = browseOrder;
  if (browseOrder.open !== open || browseOrder.scope !== scope) {
    order = captureOrder();
    setBrowseOrder(order);
  } else if (open) {
    // Lists can arrive after the drawer opens. Remember those rows too, and
    // append later arrivals without moving rows that are already on screen.
    const knownSessions = new Set(order.sessionIds);
    const knownDirectories = new Set(order.directories);
    const addedIds = activityOrder.map((s) => s.session_id)
      .filter((id) => !knownSessions.has(id));
    const addedDirectories = orderCodeDirectoryGroups(allGroups)
      .filter((key) => !knownDirectories.has(key));
    if (addedIds.length || addedDirectories.length) {
      const reveal = addedIds.includes(activeSessionId ?? "") ? activeSessionId : null;
      const revealDirectory = reveal ? sessions.find(s => s.session_id === reveal)?.cwd ?? "" : null;
      order = { ...order,
        // An explicitly created/selected chat belongs at the reachable recent
        // end, including while the desktop drawer remains open.
        sessionIds: [...addedIds.filter(id => id === activeSessionId),
          ...order.sessionIds, ...addedIds.filter(id => id !== activeSessionId)],
        directories: [...(revealDirectory !== null ? [revealDirectory] : []),
          ...order.directories.filter(key => key !== revealDirectory),
          ...addedDirectories.filter(key => key !== revealDirectory)],
        revealSessionId: reveal ?? order.revealSessionId,
      };
      setBrowseOrder(order);
    }
  }
  const sessionRanks = new Map(order.sessionIds.map((id, index) => [id, index]));
  const compareVisibleSessions = (a: SessionInfo, b: SessionInfo) => open
    ? (sessionRanks.get(a.session_id) ?? Infinity)
      - (sessionRanks.get(b.session_id) ?? Infinity)
      || compareActivity(a, b)
    : compareActivity(a, b);

  const filter = q.toLowerCase();
  const matches = (s: SessionInfo) => (
    agentFilter[s.engine ?? "dsh"] && (!filter
      || (s.summary || "").toLowerCase().includes(filter)
      || (s.first_prompt || "").toLowerCase().includes(filter)
      || (s.cwd || "").toLowerCase().includes(filter)
      || (s.native_session_id || s.session_id).toLowerCase().includes(filter))
  );
  const filtered = sessions.filter(matches);
  const archived = filtered.filter((s) => s.tag === "archived").sort(compareVisibleSessions);
  const active = filtered.filter((s) => s.tag !== "archived");
  const pinned = active.filter((s) => s.pinned).sort(compareVisibleSessions);
  const regular = active.filter((s) => !s.pinned);
  const activityRows = [...active].sort(compareVisibleSessions);
  if (bottomActivity) activityRows.reverse();
  const hasActive = active.length > 0;
  const previousView = useRef("");
  const viewKey = `${open}:${scope}:${q}:${order.revealSessionId ?? ""}`;
  // WebKit has no native overflow anchoring. Capture a visible row before the
  // React commit so prepended rows or taller status badges do not move it.
  const scrollElement = scrollRef.current;
  let anchor: { element: HTMLElement; top: number } | null = null;
  if (sessions.length <= 80 && open && bottomActivity && !scrolling.current && !pressStart.current && !lifting && menuCardId === null
      && previousView.current === viewKey && scrollElement
      && !scrollElement.querySelector(".scard.lifting")) {
    const bounds = scrollElement.getBoundingClientRect();
    for (const element of scrollElement.querySelectorAll<HTMLElement>(".scard")) {
      const row = element.getBoundingClientRect();
      if (row.bottom > bounds.top && row.top < bounds.bottom) {
        if (!anchor) anchor = { element, top: row.top };
        break;
      }
    }
  }
  useLayoutEffect(() => {
    if (anchor?.element.isConnected && scrollRef.current) {
      const delta = anchor.element.getBoundingClientRect().top - anchor.top;
      // Even assigning an unchanged scrollTop cancels WebKit momentum. Only
      // repair a real layout shift while the user is not touching/scrolling.
      if (Math.abs(delta) > .5) scrollRef.current.scrollTop += delta;
    }
    previousView.current = viewKey;
  });
  useLayoutEffect(() => {
    // Scroll once when opening/changing/searching, including the first async
    // roster. Live activity refreshes never pull a reader back to the bottom.
    if (open && bottomActivity && scrollRef.current) {
      scrollRef.current.scrollTop = bottomActivity ? scrollRef.current.scrollHeight : 0;
    }
  }, [open, mobile, bottomActivity, scope, q, hasActive, order.revealSessionId]);

  const groups: Record<string, SessionInfo[]> = {};
  for (const s of regular) {
    const key = s.cwd || "";
    (groups[key] = groups[key] || []).push(s);
  }
  for (const k of Object.keys(groups)) {
    groups[k].sort(compareVisibleSessions);
  }
  const groupKeys = orderCodeDirectoryGroups(groups);
  if (open) {
    const ranks = new Map(order.directories.map((key, index) => [key, index]));
    groupKeys.sort((a, b) => (ranks.get(a) ?? Infinity) - (ranks.get(b) ?? Infinity));
  }

  if (bottomActivity) groupKeys.reverse();

  const toggleGroup = (key: string) => setCollapsed((c) => ({ ...c, [key]: !c[key] }));
  const commitRename = (value = renaming?.value ?? "") => {
    if (renaming && value.trim()) onRename(renaming.id, value.trim());
    setRenaming(null);
  };
  const startRename = (s: SessionInfo) => {
    cancelSelect();
    setRenaming({ id: s.session_id, value: s.summary || s.first_prompt || "" });
    setMenuCardId(null); setLifting(false);
  };
  const doArchive = (s: SessionInfo, archived: boolean) => {
    onArchive(s.session_id, archived);
    setMenuCardId(null); setLifting(false);
  };
  const doPin = (s: SessionInfo) => {
    onPin(s, !s.pinned);
    setBrowseOrder({ ...captureOrder(), sessionIds: activityOrder
      .map((session) => session.session_id === s.session_id ? { ...session, pinned: !s.pinned } : session)
      .sort(compareActivity).map((session) => session.session_id) });
    setMenuCardId(null); setLifting(false);
  };
  const [copyError, setCopyError] = useState("");
  const doCopy = async (s: SessionInfo, kind: "id" | "native" | "remote") => {
    const value = kind === "native" ? nativeSessionLink(s)
      : kind === "remote" ? remoteSessionLink(s, machineId, window.location.origin)
      : s.native_session_id ?? s.session_id;
    if (!value) return;
    setCopiedId(null); setCopyError("");
    try {
      if (!navigator.clipboard?.writeText) throw new Error("unavailable");
      await navigator.clipboard.writeText(value);
      setCopyError(""); setCopiedId(`${s.session_id}:${kind}`);
      window.setTimeout(() => setCopiedId(null), 1200);
    } catch { setCopyError("无法写入剪贴板，请检查浏览器权限后重试。"); }
  };
  const doDelete = (s: SessionInfo) => {
    onDelete(s.session_id);
    setMenuCardId(null); setLifting(false);
  };
  const closeMenu = () => { setMenuCardId(null); setLifting(false); };

  // reset menu/lift state when the sidebar closes (so it doesn't linger into the next open)
  useEffect(() => {
    if (!open) {
      setMenuCardId(null);
      setLifting(false);
      setRenaming(null);
    }
    return () => {
      cancelSelect();
      if (pressTimer.current !== null) window.clearTimeout(pressTimer.current);
      pressTimer.current = null;
      pressStart.current = null;
      if (scrollIdleTimer.current !== null) window.clearTimeout(scrollIdleTimer.current);
      scrolling.current = false;
    };
  }, [open, scope]);

  // dismiss the ⋯ popover on any click outside it — covers the sidebar header, footer,
  // the side scrim, and the rest of the page (not just .s-scroll). The long-press lift
  // is dismissed via .s-lift-scrim instead, so we only handle the non-lifting popover here.
  useEffect(() => {
    if (menuCardId === null || lifting) return;
    const onDocClick = (e: MouseEvent) => {
      const t = e.target as Element | null;
      if (t && (t.closest?.(".card-menu") || t.closest?.(".scard-act"))) return;  // click inside menu / on ⋯
      setMenuCardId(null);
    };
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [menuCardId, lifting]);

  const clearPressTimer = () => {
    if (pressTimer.current !== null) window.clearTimeout(pressTimer.current);
    pressTimer.current = null;
  };
  const onListTouchStart = (e: TouchEvent) => {
    clearPressTimer();
    scrolling.current = true;
    if (scrollIdleTimer.current !== null) window.clearTimeout(scrollIdleTimer.current);
    suppressClickUntil.current = 0;
    const touch = e.touches.length === 1 ? e.touches[0] : null;
    pressStart.current = touch ? { x: touch.clientX, y: touch.clientY } : null;
  };
  const suppressGestureClick = () => {
    cancelSelect();
    clearPressTimer();
    suppressClickUntil.current = Date.now() + 700;
  };
  // long-press (500ms hold, no >10px move) => lift the card + show actions.
  // Safari may cancel a touch when native scrolling takes over. End/cancel,
  // actual scroll, sidebar close and unmount must all clear this timer.
  const onCardTouchStart = (s: SessionInfo, e: TouchEvent) => {
    if (!pressStart.current || (e.target instanceof Element
        && e.target.closest("button, input, .card-menu"))) return;
    pressTimer.current = window.setTimeout(() => {
      pressTimer.current = null;
      suppressClickUntil.current = Date.now() + 700;
      setMenuCardId(s.session_id);
      setLifting(true);
      if (navigator.vibrate) navigator.vibrate(15);
    }, 500);
  };
  const onListTouchMove = (e: TouchEvent) => {
    if (!pressStart.current) return;
    if (e.touches.length !== 1) { suppressGestureClick(); return; }
    const dx = Math.abs(e.touches[0].clientX - pressStart.current.x);
    const dy = Math.abs(e.touches[0].clientY - pressStart.current.y);
    if (dx > 10 || dy > 10) suppressGestureClick();
  };
  const onListTouchEnd = () => {
    clearPressTimer();
    pressStart.current = null;
    if (suppressClickUntil.current) suppressGestureClick();
    markScrollSettling();
  };
  const markScrollSettling = () => {
    if (scrollIdleTimer.current !== null) window.clearTimeout(scrollIdleTimer.current);
    scrollIdleTimer.current = window.setTimeout(() => { scrolling.current = false; }, 160);
  };

  const renderCard = (s: SessionInfo, _showLocation = false) => {
    const isActive = s.session_id === activeSessionId;
    const isArchived = s.tag === "archived";
    const isMenu = menuCardId === s.session_id;
    const capabilities = sessionMenuCapabilities(s);
    const onTitleClick = (detail: number) => {
      if (lifting) { closeMenu(); return; }
      if (capabilities.rename && (detail > 1 || pendingSelect.current === s.session_id)) {
        startRename(s); return;
      }
      cancelSelect();
      if (!capabilities.rename || detail === 0) { onSelect(s.session_id); return; }
      // A short double-tap window prevents the first click from closing the
      // drawer before a rename can arrive. Scrolling/closing cancels selection.
      pendingSelect.current = s.session_id;
      selectTimer.current = window.setTimeout(() => {
        selectTimer.current = null; pendingSelect.current = null; onSelect(s.session_id);
      }, 300);
    };
    // Prefer live runtime state (resident session) over the list snapshot.
    const st = liveStates?.[s.session_id] ?? s.state;
    const completion = completionBadges?.[s.session_id];
    const completionLabel = completion === "btw" ? "BTW 完成"
      : completion === "both" ? "2 项完成"
      : completion ? "已完成" : null;
    const menuRow = isMenu && scrollRef.current
      ? [...scrollRef.current.querySelectorAll<HTMLElement>(".scard")]
        .find((element) => element.dataset.sessionId === s.session_id) : null;
    const menuBounds = menuRow?.getBoundingClientRect();
    const scrollBounds = isMenu ? scrollRef.current?.getBoundingClientRect() : null;
    const aboveSpace = menuBounds && scrollBounds ? menuBounds.top + 28 - scrollBounds.top : 0;
    const belowSpace = menuBounds && scrollBounds ? scrollBounds.bottom - menuBounds.top - 36 : 0;
    const menuAbove = aboveSpace > belowSpace;
    const menuMaxHeight = scrollBounds ? Math.max(64, (menuAbove ? aboveSpace : belowSpace) - 8) : undefined;
    return (
      <div
        key={s.session_id}
        data-session-id={s.session_id}
        className={"scard" + (isActive ? " active" : "") + (isArchived ? " archived" : "") + (isMenu ? " menu-open" : "") + (isMenu && lifting ? " lifting" : "")}
        onTouchStart={(e) => onCardTouchStart(s, e)}
        onClick={event => onTitleClick(event.detail)}
        onDoubleClick={event => {
          if (event.target instanceof Element && event.target.closest("button,input,.card-menu")) return;
          if (capabilities.rename) { event.preventDefault(); startRename(s); }
        }}
      >
        <div className="scard-top">
          <span className={"scard-title" + (expandedTitles[s.session_id] ? " full-title" : "")}>
            {s.summary || s.first_prompt || s.session_id.slice(0, 8)}
          </span>
        {(s.summary || s.first_prompt || "").length > 48 && <button type="button" className="scard-title-toggle" aria-expanded={!!expandedTitles[s.session_id]}
          aria-label={expandedTitles[s.session_id] ? "收起标题" : "完整标题"}
          onClick={(event) => { event.stopPropagation(); setExpandedTitles((current) => ({ ...current, [s.session_id]: !current[s.session_id] })); }}>
          <Icon name="chev" size={12} />
        </button>}
        </div>
        <div className="scard-meta">
          {s.cwd && <div className="scard-location" title={s.cwd}>
            <Icon name="folder" size={12} /><span>{basename(s.cwd)}</span>
          </div>}
          <AgentBadge engine={s.engine} className="scard-engine" />
          {s.pinned && <Icon name="pin" size={12} />}
        </div>
        <div className="scard-status">
          {(st === "running" || st === "interrupting") && (
            <span className={"pill " + st}><span className="sd" />{st === "running" ? "运行" : "中断"}</span>
          )}
          {completionLabel && st !== "running" && st !== "interrupting" && (
            <span className="pill completed"><span className="sd" />{completionLabel}</span>
          )}
          {st !== "running" && st !== "interrupting" && !completionLabel && <span className="session-idle-label">已完成</span>}
          {isActive && <span className="pill idle"><span className="sd" />当前</span>}
          {activityLabel(s.last_modified)}
        </div>

        <div className="scard-actions">
          <button className="scard-act" aria-label="更多操作"
            onClick={(e) => { e.stopPropagation(); setMenuCardId(isMenu ? null : s.session_id); setLifting(false); }}>
            <Icon name="dots" size={15} />
          </button>
        </div>
        {isMenu && (
          <div className={"card-menu" + (menuAbove ? " above" : "")} style={{maxHeight:menuMaxHeight}} onClick={(e) => e.stopPropagation()}>
            {capabilities.rename && (
              <button onClick={() => startRename(s)}><Icon name="edit" size={15} />重命名</button>
            )}
            <button onClick={() => doPin(s)}>
              <Icon name="pin" size={15} />{s.pinned ? "取消置顶" : "置顶"}
            </button>
            {nativeSessionLink(s) && <button onClick={() => void doCopy(s, "native")} title="在拥有此会话的电脑上打开 Codex 应用"><Icon name={copiedId === `${s.session_id}:native` ? "check" : "copy"} size={15} />{copiedId === `${s.session_id}:native` ? "已复制 Codex 链接" : "复制 Codex 链接"}</button>}
            {remoteSessionLink(s, machineId, window.location.origin) && <button onClick={() => void doCopy(s, "remote")} title="通过 Amo 打开此设备的会话，仍需登录"><Icon name={copiedId === `${s.session_id}:remote` ? "check" : "copy"} size={15} />{copiedId === `${s.session_id}:remote` ? "已复制 Amo 链接" : "复制 Amo 会话链接"}</button>}
            <button onClick={() => void doCopy(s, "id")}><Icon name={copiedId === `${s.session_id}:id` ? "check" : "copy"} size={15} />{copiedId === `${s.session_id}:id` ? "已复制" : "复制 session ID"}</button>
            {copyError && <p className="session-copy-error" role="alert">{copyError}</p>}
            {s.cwd && <button onClick={() => { onNewInDir(s.cwd!); closeMenu(); }}><Icon name="plus" size={15} />在此目录新建</button>}
            {capabilities.archive && (isArchived ? (
              <button onClick={() => doArchive(s, false)}><Icon name="archive" size={15} />取消归档</button>
            ) : (
              <button onClick={() => doArchive(s, true)}><Icon name="archive" size={15} />归档</button>
            ))}
            {capabilities.delete && (
              <button className="danger" onClick={() => doDelete(s)}>
                <Icon name="trash" size={15} />删除会话
              </button>
            )}
          </div>
        )}
      </div>
    );
  };

  const renderGroup = (key: string, items: SessionInfo[], label?: string, windowRows?: SidebarWindowRow[]) => {
    // While searching, expand every group so matches in collapsed/archived groups are visible.
    const isCollapsed = filter ? false : collapsed[key];
    const title = label || basename(key);
    const isCodeDirectoryGroup = !label;
    const canAdd = isCodeDirectoryGroup && !!key;
    const showDirectoryPath = isCodeDirectoryGroup && !!key;
    const showPinnedLocation = key === "__pinned__";
    // A directory's last activity also includes its pinned sessions, even
    // when they are shown in the separate pinned section.
    const directoryLatest = isCodeDirectoryGroup
      ? activityByDirectory.get(key) ?? -Infinity
      : null;
    const directoryExpanded = !!expandedDirectories[key];
    const visibleItems = isCodeDirectoryGroup
      ? visibleDirectorySessions(items, directoryExpanded, !!filter)
      : items;
    const displayItems = bottomActivity ? [...visibleItems].reverse() : visibleItems;
    const hiddenCount = items.length - visibleItems.length;
    const renderHead = () => (
        <div className="sgroup-head">
          <button className={"sgroup-toggle" + (isCollapsed ? " collapsed" : "")}
            aria-label={showDirectoryPath ? `${title}，${key}` : undefined}
            onClick={() => toggleGroup(key)}>
            <span className={"sgroup-icon " + (isCodeDirectoryGroup ? "folder-icon" : "chev")}>
              <Icon name={isCodeDirectoryGroup ? (isCollapsed ? "folder" : "folder-open") : "chev"}
                size={isCodeDirectoryGroup ? 15 : 13} />
            </span>
            <span className="label">{title}</span>
            <span className="count">{items.length}</span>
            {directoryLatest !== null && activityLabel(Number.isFinite(directoryLatest) ? directoryLatest : null, true)}
          </button>
          {canAdd && (
            <button className="sgroup-add" title={`在 ${title} 新建会话`} aria-label="在此目录新建会话"
              onClick={(e) => { e.stopPropagation(); onNewInDir(key); }}>
              <Icon name="plus" size={14} />
            </button>
          )}
          {showDirectoryPath && <span className="sgroup-path-tip" role="tooltip">{key}</span>}
        </div>
    );
    const renderMore = () => (
            <button className="sgroup-more" onClick={(event) => {
              event.stopPropagation();
              setExpandedDirectories((current) => ({
                ...current, [key]: !directoryExpanded,
              }));
            }}>
              {directoryExpanded ? "收起" : `展开其余 ${hiddenCount} 个会话`}
            </button>
    );
    const hasMore = isCodeDirectoryGroup && !filter && items.length > 5;
    if (windowRows) {
      windowRows.push({ key: `group:${key}`, kind: "head", render: renderHead });
      if (!isCollapsed) {
        for (const session of displayItems) windowRows.push({
          key: session.session_id, kind: "card", indented: true,
          render: () => renderCard(session, showPinnedLocation),
        });
        if (hasMore) windowRows.push({ key: `more:${key}`, kind: "more", indented: true, render: renderMore });
      }
      return null;
    }
    return (
      <div key={key || "other"} className="sgroup-wrap">
        {renderHead()}
        {!isCollapsed && <div className="sgroup-items">
          {displayItems.map((session) => renderCard(session, showPinnedLocation))}
          {hasMore && renderMore()}
        </div>}
      </div>
    );
  };

  const windowRows: SidebarWindowRow[] = [];
  if (archived.length > 0) renderGroup("archived", archived, "已归档", windowRows);
  if (listView === "activity") {
    for (const session of activityRows) windowRows.push({
      key: session.session_id, kind: "card", render: () => renderCard(session, true),
    });
  } else {
    for (const key of groupKeys) renderGroup(key, groups[key], undefined, windowRows);
    if (pinned.length > 0) renderGroup("__pinned__", pinned, "置顶", windowRows);
  }
  // Keep the window mounted when filtering/expanding changes its length. A
  // mid-visit switch between two scroll owners would discard the reading spot.
  const windowed = sessions.length > 80;

  return (
    <>
      <div className={"scrim-side" + (open ? " show" : "")} onClick={onClose} />
      <aside className={"sessions" + (open ? " show" : "")} data-side={drawerSide} aria-hidden={!open} inert={!open}>
        {/* Keep sidebar contents stable while the desktop shell and panel slide
            in tandem; mobile uses the same panel transform as an overlay. */}
        <div className="s-inner">
          <div className="s-head">
            <div className="brand" onClick={onClose}>
              <span className="brand-mark"><BrandMark size={30} /></span>
              <span className="name"><b>Amo</b><span>阿萌 · Agent Remotify</span></span>
            </div>
            <button className="iconbtn" onClick={onClose} aria-label="收起"><Icon name="chevrons-left" /></button>
          </div>
          <div className="s-view-switch" role="group" aria-label="会话排序视图">
            <button className={listView === "activity" ? "active" : ""} onClick={() => setListView("activity")}>最近活跃</button>
            <button className={listView === "directory" ? "active" : ""} onClick={() => setListView("directory")}>项目目录</button>
          </div>
          <div className="search">
            <Icon name="search" size={17} />
            <input value={q} onChange={(e) => setQ(e.target.value)}
              placeholder="搜索会话、路径…"
              aria-label="搜索会话" />
          </div>
          <div ref={scrollRef} className={"s-scroll" + (bottomActivity ? " activity-bottom" : "")} data-lock-horizontal-swipe
            onTouchStartCapture={onListTouchStart}
            onTouchMoveCapture={onListTouchMove}
            onTouchEndCapture={onListTouchEnd}
            onTouchCancelCapture={() => {
              suppressGestureClick();
              pressStart.current = null;
              if (lifting) closeMenu();
              markScrollSettling();
            }}
            onWheelCapture={() => { cancelSelect(); scrolling.current = true; markScrollSettling(); }}
            onScroll={() => {
              if (scrolling.current) markScrollSettling();
              if (pressStart.current || Date.now() < suppressClickUntil.current) {
                suppressGestureClick();
              }
              if (lifting) closeMenu();
            }}
            onClickCapture={(e) => {
              // Keyboard activation (detail=0) remains available. A fresh tap
              // resets the guard in touchstart, so there is no dead tap window.
              if (e.detail > 0 && Date.now() < suppressClickUntil.current) {
                e.preventDefault();
                e.stopPropagation();
              }
            }}
            onClick={closeMenu}>
            {filtered.length === 0 && <div className="s-group">{sessions.length === 0 ? "暂无会话" : "无匹配"}</div>}
            {windowed ? <SidebarWindow rows={windowRows} scrollRef={scrollRef} open={open}
              visit={viewKey} /> : <>
            {bottomActivity && archived.length > 0 && renderGroup("archived", archived, "已归档")}
            {listView === "activity" ? <div className="s-activity-list">{activityRows.map((session) => renderCard(session, true))}</div> : <div className="s-directory-list">
              {!bottomActivity && pinned.length > 0 && renderGroup("__pinned__", pinned, "置顶")}
              {groupKeys.map((k) => renderGroup(k, groups[k]))}
              {bottomActivity && pinned.length > 0 && renderGroup("__pinned__", pinned, "置顶")}
            </div>}
            {!bottomActivity && archived.length > 0 && renderGroup("archived", archived, "已归档")}
            </>}
            <div className={"s-lift-scrim" + (lifting ? " show" : "")} onClick={closeMenu} />
          </div>
          <div className="s-foot">
          <div className="agent-filters" role="group" aria-label="筛选 Agent 类型">
            {agentTypes.map(kind => <label key={kind} className={`agent-filter-chip agent-${kind}`}>
              <input type="checkbox" checked={agentFilter[kind]}
                aria-label={kind === "dsh" ? "显示 DSH 会话" : "显示 Codex 会话"}
                onChange={event => setAgentFilter(current => ({ ...current, [kind]:event.target.checked }))} />
              <AgentBadge engine={kind} />
              <span className="agent-filter-check" aria-hidden="true"><Icon name="check" size={13} /></span>
            </label>)}
          </div>
            <button className="newbtn" onClick={() => onNew()}><Icon name="plus" size={19} />新会话</button>
          </div>
        </div>
      </aside>
      {renaming && <RenameSessionDialog key={renaming.id} title={renaming.value} onSave={commitRename} onClose={() => setRenaming(null)} />}
    </>
  );
}
