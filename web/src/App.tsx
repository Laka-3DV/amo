import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
} from "react";
import { RelayWs, sessionScopeKey, type EventOwnership } from "./ws";
import {
  createRuntime,
  deferredQueueCapacity,
  initialState,
  modelCatalogScopeKey,
  reduce,
  type PendingQuery,
  type PreviewAuthorizationState,
} from "./reducer";
import type { Turn } from "./domain/conversation";
import { APPEARANCE_KEY, readAppearance, appearanceLayout, type Appearance } from "./appearance";
import { uuid } from "./util";
import { Icon } from "./icons";
import { ChatView } from "./components/ChatView";
import { Composer } from "./components/Composer";
import {
  QueuedQueryDialog,
  type QueuedQueryEditor,
} from "./components/QueuedQueryDialog";
import { ReconnectBanner } from "./components/ReconnectBanner";
import { NoticeStack } from "./components/NoticeStack";
import { presentCommandProblem } from "./problem-presentation";
import { LoginForm } from "./components/LoginForm";
import { retainCreatedSession, type CreatedSessionFocus } from "./created-session-focus";
import { SessionsSidebar } from "./components/SessionsSidebar";
import { FileBrowser } from "./components/FileBrowser";
import { DirPicker } from "./components/DirPicker";
import {
  compatibleNewChatEffort,
  newChatCatalogRequest,
  NewChatView,
  reconcileNewChatSelection,
  resolveNewChatLocalDefaults,
} from "./components/NewChatView";
import { QuestionSheet } from "./components/QuestionSheet";
import { CapabilitiesSheet, type SkillDraft } from "./components/CapabilitiesSheet";
import { DeviceSheet, type PairingState, type RemoteDevice } from "./components/DeviceSheet";
import { AgentBadge } from "./components/AgentBadge";
import { useSidebarGesture } from "./use-sidebar-gesture";
import { HeaderMenu } from "./components/HeaderMenu";
import { dshHeaderPresetLabel, permsFor } from "./data";
import {
  normalizeSessionList,
  mergedAgentSessions,
  updateScopedSessionLifecycle,
} from "./session-list";
import { clearLegacyAuthMarkers, probeSession } from "./session-auth";
import { nextAutoLoadDetailTurn } from "./history-detail-projection";
import {
  canEnqueueQuery,
  collectUnconfirmedQueries,
} from "./runtime-drain";
import { MAX_RUNTIME_SESSIONS } from "./runtime-bounds";
import {
  FORK_FOCUS_REFRESH_MS,
  forkFocusLeaseSession,
  matchesSessionForkRequest,
  type ForkFocusLease,
  type PendingSessionFork,
  withoutForkFocusPlaceholder,
} from "./session-worktree";
import {
  normalizeDiffTheme,
  type Snapshot, type QueryImg,
  type QueryFile, type SessionInfo,
  type DiffTheme, type Engine, type Space, type FilesList,
  type SessionControl, type History, sessionControlLocksInput,
  type EngineCapabilities, type EngineCapabilityItem, type EngineCapabilityKind,
  type AccountUsageReport,
} from "./protocol";
import { isMarkdownPath } from "./preview-path";
import { parseGitDiff } from "./diff";
import { useDeviceLayout } from "./use-device-layout";
import { readDeviceLayout } from "./responsive-layout";
import {
  bumpSessionActivity,
  compareSessionsByActivity,
  mergeSessionActivityState,
  sessionCommandTarget,
  setSessionPinned,
} from "./session-order";
import {
  disableRemotePush,
  enableRemotePush,
  PushBindingCoordinator,
  type PushBindingSnapshot,
} from "./push";
import {
  protectedHistoryTurnIds,
  type TextSelectionGuard,
} from "./history-selection-guard";
import {
  readNotificationMode,
  writeNotificationMode,
  type NotificationMode,
} from "./notification-mode";
import {
  captureNotificationFragment,
  consumeNotificationTarget,
  encodeNotificationRoute,
  NOTIFICATION_TARGET_KEY,
  parseNotificationRoute,
  storeNotificationTarget,
  type NotificationRoute,
} from "./notification-route";
import {
  resolveNotificationNavigation,
  type NotificationOrigin,
} from "./notification-navigation";
import {
  turnNotificationPresentation,
  turnNotificationTag,
} from "./turn-notification";
import {
  HistoryRequestCoordinator,
  HistoryDetailRequestCoordinator,
  resolveHistoryCwdHint,
  type CancelledHistoryBrowseRequest,
  type HistoryBrowseRequestContext,
  type HistoryDetailRequestContext,
} from "./history-requests";
import { RecoverableReadCoordinator } from "./recoverable-read";
import { InlineImageAssetCache } from "./inline-image-assets";
import { HistoryImageAssetCache } from "./history-image-assets";
import {
  displayHistoryProjection,
  historyConfirmsRecovery,
  historyConfirmsRuntimeRecovery,
  historyNeedsConfirmationRequest,
  isHistoryRecoveryPending,
  isRuntimeHistoryRecoveryPending,
} from "./history-recovery";
import {
  acceptsCachedNewerPage,
  appendNewerPage,
  canonicalTurnId,
  prependOlderPage,
  type HistoryBrowsePage,
} from "./history-browse";
import {
  HistoryPageCache,
  historyPageCacheScopeKey,
  type HistoryPageCacheScope,
  type HistoryPageCacheSessionScope,
} from "./history-page-cache";
import {
  HISTORY_INITIAL_PAGE,
  HISTORY_LATEST_PAGE_KEY,
  HISTORY_MORE_PAGE,
  HISTORY_PROVISIONAL_WATCHDOG_MS,
  historyPageKey,
  summaryHistoryTurns,
} from "./history-summary";
import { browserDraftPersistence } from "./draft-persistence";
import { ComposerDraftStore, composerDraftKey, draftHasContent } from "./composer-drafts";
import {
  acknowledgeCompletion,
  acknowledgeMatchingCompletion,
  catalogCompletionProjection,
  completionAcknowledgementId,
  completionBadgeKind,
  discardBtwCompletionReceipts,
  markCompletionUnread,
  newestCompletionProjection,
  rekeyCompletionReceipts,
  type CompletionBadgeKind,
  type CompletionReceipts,
} from "./completion-badges";
import {
  cacheSkillCatalog,
  SkillCatalogRequestCoordinator,
  skillCatalogFresh,
  skillCatalogKey,
  skillCatalogRefreshSucceeded,
  type SkillCatalogCacheEntry,
  type SkillCatalogRequest,
} from "./skill-catalog-cache";
import {
  ENGINE_SPACES_KEY,
  LEGACY_SPACE_KEY,
  readEngineSpaces,
  rememberEngineSpace,
  type EngineSpaces,
} from "./surface-preferences";

const THEME_KEY = "cc_remote_theme";
const ENGINE_KEY = "cc_remote_engine";  // which backend the NEXT new session uses
const MACHINE_KEY = "cc_remote_machine";
const ArtifactPanel = lazy(() => import("./components/ArtifactPanel").then(
  ({ ArtifactPanel: Panel }) => ({ default: Panel }),
));

const AccountUsageSheet = lazy(() => import("./components/AccountUsageSheet").then(
  ({ AccountUsageSheet: Sheet }) => ({ default: Sheet }),
));

interface QueuedQueryEditorState extends QueuedQueryEditor {
  detailRequestId: string | null;
  updateRequestId: string | null;
  pendingPrompt: string | null;
  pendingStartNow?: boolean;
  takeback?: boolean;
}

// Phones and touch tablets use an overlay; desktop uses a persistent column.
// Close the overlay after picking a session.
const isMobile = () => readDeviceLayout().overlay;

export default function App() {
  const deviceLayout=useDeviceLayout();
  const [appearance, setAppearance] = useState<Appearance>(() => readAppearance(localStorage));
  const [chatSpacing, setChatSpacing] = useState<"compact" | "standard" | "relaxed">(() => {
    const saved = localStorage.getItem("cc_remote_chat_spacing");
    return saved === "standard" || saved === "relaxed" ? saved : "compact";
  });
  const [theme, setTheme] = useState<DiffTheme>(
    () => normalizeDiffTheme(localStorage.getItem(THEME_KEY)));
  const initialEngineRef = useRef<Engine>(localStorage.getItem(ENGINE_KEY) === "codex" ? "codex" : "dsh");
  const initialSpacesRef = useRef(readEngineSpaces(localStorage, initialEngineRef.current));
  const [engine, setEngine] = useState<Engine>(initialEngineRef.current);
  const [space, setSpace] = useState<Space>("code");
  const spacesByEngineRef = useRef<EngineSpaces>(initialSpacesRef.current);
  const [authed, setAuthed] = useState(false);
  const [authReady, setAuthReady] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [accountUsageOpen, setAccountUsageOpen] = useState(false);
  const [accountUsageReports, setAccountUsageReports] = useState<Partial<Record<"dsh" | "codex", AccountUsageReport>>>({});
  const accountUsageRequests = useRef<Partial<Record<"dsh" | "codex", string>>>({});
  const [accountUsageSupported, setAccountUsageSupported] = useState(false);
  const [queuedAttachmentsSupported, setQueuedAttachmentsSupported] = useState(false);
  const [queuedEditHoldSupported, setQueuedEditHoldSupported] = useState(false);
  const drawer = useSidebarGesture(deviceLayout, sidebarOpen, setSidebarOpen);
  const [fileBrowserOpen, setFileBrowserOpen] = useState(false);
  const [filesList, setFilesList] = useState<FilesList | null>(null);
  const [dirPickerOpen, setDirPickerOpen] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [newChatAutoFocus, setNewChatAutoFocus] = useState(true);
  const [editPrompt, setEditPrompt] = useState<string | null>(null);
  const [queuedTakeback, setQueuedTakeback] = useState<PendingQuery | null>(null);
  const [queuedQueryEditor, setQueuedQueryEditor] =
    useState<QueuedQueryEditorState | null>(null);
  const queuedEditSid = queuedQueryEditor?.sid;
  const queuedEditMsgId = queuedQueryEditor?.msgId;
  const queuedEditHeld = queuedQueryEditor?.editingHeld;
  const queuedEditState = queuedQueryEditor?.state;
  useEffect(() => {
    if (!queuedEditHeld || queuedEditState !== "queued" || !queuedEditSid || !queuedEditMsgId) return;
    const renew = () => {
      const requestId = wsRef.current?.sendGetQueuedQueryTo(queuedEditSid, queuedEditMsgId, "renew");
      if (requestId) setQueuedQueryEditor(editor => editor?.sid === queuedEditSid && editor.msgId === queuedEditMsgId
        ? {...editor, detailRequestId: requestId} : editor);
    };
    const timer = window.setInterval(renew, 20_000);
    const onVisible = () => { if (document.visibilityState === "visible") renew(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      wsRef.current?.sendGetQueuedQueryTo(queuedEditSid, queuedEditMsgId, "end");
    };
  }, [queuedEditSid, queuedEditMsgId, queuedEditHeld, queuedEditState]);
  const [capabilitiesOpen, setCapabilitiesOpen] = useState(false);
  const [capabilitiesKind, setCapabilitiesKind] = useState<EngineCapabilityKind | "all">("all");
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(false);
  const [deviceSheetOpen, setDeviceSheetOpen] = useState(false);
  // Ask_id whose answer is in flight. The card stays until the wrapper closes
  // it, so a frame lost to a reconnect is visible and retryable.
  const [askAnswerPending, setAskAnswerPending] = useState<string | null>(null);
  const [askCancelPending, setAskCancelPending] = useState<string | null>(null);
  const [capabilitiesByScope, setCapabilitiesByScope] =
    useState<Record<string, EngineCapabilities>>({});
  const [, setSkillCatalogs] =
    useState<Record<string, SkillCatalogCacheEntry>>({});
  const [notificationMode, setNotificationMode] = useState<NotificationMode>(
    () => readNotificationMode(localStorage));
  const [pushBinding, setPushBinding] = useState<PushBindingSnapshot>({
    state: "off", target: null, bound: null,
  });
  const [machineId, setMachineId] = useState(
    () => localStorage.getItem(MACHINE_KEY) || "default");
  useEffect(() => {
    if (!authed) { setAccountUsageSupported(false); setQueuedAttachmentsSupported(false); setQueuedEditHoldSupported(false); return; }
    let cancelled = false;
    const check = async () => {
      try {
        const response = await fetch("/healthz", {cache:"no-store"});
        const data = response.ok ? await response.json() : null;
        if (!cancelled) {
          setAccountUsageSupported(data?.capabilities?.includes("account_usage") === true);
          setQueuedAttachmentsSupported(data?.capabilities?.includes("queued_attachments") === true);
          setQueuedEditHoldSupported(data?.capabilities?.includes("queued_edit_hold") === true);
        }
      } catch { /* Existing connection state handles a transient network loss. */ }
    };
    void check();
    const timer = window.setInterval(() => void check(), 15000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [authed, machineId]);
  useEffect(() => {
    setAccountUsageOpen(false);
    setAccountUsageReports({});
    accountUsageRequests.current = {};
  }, [machineId, authed]);
  const [remoteDevices, setRemoteDevices] = useState<RemoteDevice[]>([]);
  const [devicesLoadState, setDevicesLoadState] = useState<
    "idle" | "loading" | "ready" | "error"
  >("idle");
  const [pendingNotificationTarget, setPendingNotificationTarget] = useState<
    NotificationRoute | null
  >(() => {
    let captured: NotificationRoute | null = null;
    try {
      captured = captureNotificationFragment(
        window.location, sessionStorage, window.history);
    } catch {
      // Keep trying the in-memory capture below.
    }
    try {
      return consumeNotificationTarget(sessionStorage) ?? captured;
    } catch {
      return captured;
    }
  });
  const [notificationListRevision, bumpNotificationListRevision] = useReducer(
    (value: number) => value + 1, 0);
  const [devicePairing, setDevicePairing] = useState<PairingState>({
    enabled: false, expires_at: null,
  });
  const [completionReceipts, setCompletionReceipts] = useState<CompletionReceipts>({});
  const completionReceiptsRef = useRef(completionReceipts);
  completionReceiptsRef.current = completionReceipts;
  const [state, dispatch] = useReducer(reduce, initialState);
  const inlineImageAssetsRef = useRef(new InlineImageAssetCache());
  const [, bumpInlineImageRevision] = useReducer((value: number) => value + 1, 0);
  const historyImageAssetsRef = useRef(new HistoryImageAssetCache());
  const [composerDraftStore]=useState(()=>new ComposerDraftStore(browserDraftPersistence()));
  const composerDraftsRef=useRef(composerDraftStore);
  const [draftsReady,setDraftsReady]=useState(false);
  useEffect(()=>{void composerDraftStore.ready.then(()=>setDraftsReady(true));},[composerDraftStore]);
  useEffect(()=>{
    const save=()=>{void composerDraftStore.flush();};
    const hidden=()=>{if(document.visibilityState==="hidden")save();};
    window.addEventListener("pagehide",save);document.addEventListener("visibilitychange",hidden);
    return()=>{window.removeEventListener("pagehide",save);document.removeEventListener("visibilitychange",hidden);save();};
  },[composerDraftStore]);
  const [, bumpHistoryImageRevision] = useReducer((value: number) => value + 1, 0);
  const dismissBanner = useCallback((banner: string) => {
    dispatch({ type: "dismiss_banner", banner });
  }, []);
  const pushCoordinatorRef = useRef<PushBindingCoordinator | null>(null);
  if (pushCoordinatorRef.current === null) {
    pushCoordinatorRef.current = new PushBindingCoordinator(async (target) => (
      target
        ? enableRemotePush(target.machineId, target.mode)
        : disableRemotePush()
    ));
  }
  const stateRef = useRef(state);
  stateRef.current = state;
  const wsRef = useRef<RelayWs | null>(null);
  const wsLifecycleEpochRef = useRef(0);
  const skillCatalogsRef =
    useRef<Record<string, SkillCatalogCacheEntry>>({});
  const skillCatalogRequestsRef =
    useRef<SkillCatalogRequestCoordinator | null>(null);
  if (skillCatalogRequestsRef.current === null) {
    skillCatalogRequestsRef.current = new SkillCatalogRequestCoordinator(
      (request) => wsRef.current?.sendGetEngineCapabilities(
        request.engine,
        request.space,
        request.cwd,
        request.skillsOnly,
        request.dshSessionId,
      ) ?? null,
    );
  }
  const focusedSkillScopeRef = useRef<{
    key: string;
    engine: Engine;
    space: Space;
    cwd: string;
    skillsOnly: boolean;
    dshSessionId?: string | null;
  } | null>(null);
  const historyRequestsRef = useRef(new HistoryRequestCoordinator());
  const historyDetailRequestsRef = useRef(new HistoryDetailRequestCoordinator(
    (context) => {
      dispatch({ type: "history_detail_cancelled", context });
    },
  ));
  const clearHistoryDetailRequests = useCallback(() => {
    for (const context of historyDetailRequestsRef.current.clear()) {
      dispatch({ type: "history_detail_cancelled", context });
    }
  }, []);
  const settleCancelledHistoryBrowse = useCallback((
    cancelled: readonly CancelledHistoryBrowseRequest[],
  ) => {
    for (const request of cancelled) {
      const browse = request.browse;
      dispatch({
        type: "history_browse_page_failed",
        sid: request.sid,
        scopeKey: browse.scopeKey,
        revision: request.revision,
        generation: request.generation,
        viewId: browse.viewId,
        windowEpoch: browse.windowEpoch,
        before: browse.pendingBefore,
      });
    }
  }, []);
  const historyPageCacheRef = useRef(new HistoryPageCache());
  const historyPageScopesRef =
    useRef(new Map<string, HistoryPageCacheScope>());
  const textSelectionGuardRef = useRef<TextSelectionGuard | null>(null);
  const updateTextSelectionGuard = useCallback(
    (guard: TextSelectionGuard | null) => {
      textSelectionGuardRef.current = guard;
    },
    [],
  );
  const createdSessionFocusRef = useRef<CreatedSessionFocus | null>(null);
  const pendingCreateRef = useRef<string | null>(null);
  const createRequestsRef = useRef<Map<string, {
    scopeKey: string;
    cwdSource: "default" | "inherited" | "explicit";
    draftKey: string;
  }>>(new Map());
  const pendingSessionForkRef = useRef<PendingSessionFork | null>(null);
  const forkFocusLeaseRef = useRef<ForkFocusLease | null>(null);
  const forkFocusLeaseTimerRef = useRef<number | null>(null);
  const sessionListsBySurfaceRef = useRef<Record<string, SessionInfo[]>>({});
  const historySessionListsRef = useRef<Record<string, SessionInfo[]>>({});
  const authoritativeSurfaceListsRef = useRef<Set<string>>(new Set());
  const sessionActivityPendingRef = useRef<Set<string>>(new Set());
  const prefetchedSurfacesRef = useRef<Set<string>>(new Set());
  const lastFocusBySurfaceRef = useRef<Record<string, string>>({});
  const preferredSurfaceFocusRef = useRef<{ key: string; sid: string } | null>(null);
  const historyInvalidationsRef = useRef<Map<string, string | null>>(new Map());
  const historyInvalidationGenerationsRef =
    useRef<Map<string, string | null>>(new Map());
  const historyCacheEpochRef = useRef<Map<string, number>>(new Map());
  const previousMachineRef = useRef(machineId);
  const notificationListRequestRef = useRef<string | null>(null);
  const notificationOriginRef = useRef<NotificationOrigin | null>(null);
  const pendingNotificationErrorRef = useRef<string | null>(null);
  const artifactDirtyRef = useRef(false);
  const setArtifactDirty = useCallback((dirty: boolean) => {
    artifactDirtyRef.current = dirty;
  }, []);
  const confirmArtifactDiscard = useCallback(() => {
    if (!artifactDirtyRef.current) return true;
    if (!window.confirm("Markdown 有未保存的修改，确定放弃吗？")) return false;
    artifactDirtyRef.current = false;
    return true;
  }, []);
  const clearForkFocusLease = useCallback((
    refresh = false,
    dropPlaceholder = true,
  ) => {
    const lease = forkFocusLeaseRef.current;
    if (forkFocusLeaseTimerRef.current !== null) {
      window.clearTimeout(forkFocusLeaseTimerRef.current);
      forkFocusLeaseTimerRef.current = null;
    }
    if (!lease) return;
    forkFocusLeaseRef.current = null;
    if (!dropPlaceholder) return;
    const surfaceKey = `${lease.space}:${lease.engine}`;
    const current = sessionListsBySurfaceRef.current[surfaceKey] ?? [];
    const sessions = withoutForkFocusPlaceholder(current, lease);
    sessionListsBySurfaceRef.current[surfaceKey] = sessions;
    historySessionListsRef.current[surfaceKey] = sessions;
    dispatch({
      type: "drop_fork_placeholder",
      sid: lease.childSessionId,
      parentSid: lease.parentSessionId,
    });
    if (refresh) wsRef.current?.sendListSessions(lease.engine, lease.space);
  }, []);
  const startForkFocusLease = useCallback((lease: ForkFocusLease) => {
    // Replacing one successful fork with another is explicit navigation. The
    // old synthetic row no longer has a lease and must not linger undeletable.
    clearForkFocusLease(false, true);
    forkFocusLeaseRef.current = lease;
    const surfaceKey = `${lease.space}:${lease.engine}`;
    const current = sessionListsBySurfaceRef.current[surfaceKey] ?? [];
    const placeholder = forkFocusLeaseSession(lease, current,
      lease.machineId, lease.engine, lease.space);
    if (placeholder) {
      const sessions = [placeholder, ...current];
      sessionListsBySurfaceRef.current[surfaceKey] = sessions;
      historySessionListsRef.current[surfaceKey] = sessions;
      dispatch({ type: "restore_session_list",
        sessions: mergedAgentSessions(sessionListsBySurfaceRef.current) });
    }
    const refresh = () => {
      const current = forkFocusLeaseRef.current;
      if (current?.requestId !== lease.requestId
          || current.childSessionId !== lease.childSessionId) return;
      wsRef.current?.sendListSessions(current.engine, current.space);
      current.refreshAt = Date.now() + FORK_FOCUS_REFRESH_MS;
      forkFocusLeaseTimerRef.current = window.setTimeout(
        refresh, FORK_FOCUS_REFRESH_MS);
    };
    forkFocusLeaseTimerRef.current = window.setTimeout(
      refresh, Math.max(0, lease.refreshAt - Date.now()));
  }, [clearForkFocusLease]);
  useEffect(() => () => {
    if (forkFocusLeaseTimerRef.current !== null) {
      window.clearTimeout(forkFocusLeaseTimerRef.current);
    }
  }, []);
  const requestHistory = useCallback((
    sid: string,
    before: string | null | undefined,
    limit: number,
    generation?: string | null,
    revision?: string | null,
    browse?: HistoryBrowseRequestContext,
  ) => {
    const ws = wsRef.current;
    if (!ws) return false;
    return historyRequestsRef.current.request({
      sid, before, limit,
      generation: generation ?? ws.generationFor(sid),
      revision,
      browse,
    }, () => ws.sendGetHistory(
      sid,
      before,
      limit,
      resolveHistoryCwdHint(historySessionListsRef.current, sid),
    ), settleCancelledHistoryBrowse);
  }, [settleCancelledHistoryBrowse]);
  const cancelPendingNotificationTarget = useCallback(() => {
    setPendingNotificationTarget(null);
    notificationListRequestRef.current = null;
    notificationOriginRef.current = null;
    try { sessionStorage.removeItem(NOTIFICATION_TARGET_KEY); }
    catch { /* private browsing storage may be unavailable */ }
  }, []);
  // guards the once-per-connection "land on the latest session" auto-focus below
  const didInitFocusRef = useRef(false);
  const shortcutRef = useRef<{
    artifact: typeof state.artifact;
    getDiff: (file: string) => void;
  }>({ artifact: null, getDiff: () => {} });

  useEffect(() => {
    const previous = previousMachineRef.current;
    if (previous === machineId) return;
    previousMachineRef.current = machineId;
    localStorage.setItem(MACHINE_KEY, machineId);
    pendingCreateRef.current = null;
    createdSessionFocusRef.current = null;
    createRequestsRef.current.clear();
    clearForkFocusLease(false);
    setQueuedQueryEditor(null);
    // Draft keys already isolate machines; keep recovery content on navigation.
    setCompletionReceipts({});
    sessionListsBySurfaceRef.current = {};
    historySessionListsRef.current = {};
    preferredSurfaceFocusRef.current = null;
    authoritativeSurfaceListsRef.current.clear();
    notificationListRequestRef.current = null;
    sessionActivityPendingRef.current.clear();
    skillCatalogsRef.current = {};
    skillCatalogRequestsRef.current?.reset();
    setSkillCatalogs({});
    setCapabilitiesByScope({});
    historyRequestsRef.current.clear();
    clearHistoryDetailRequests();
    historyPageScopesRef.current.clear();
    prefetchedSurfacesRef.current.clear();
    historyInvalidationsRef.current.clear();
    historyInvalidationGenerationsRef.current.clear();
    historyCacheEpochRef.current.clear();
    inlineImageAssetsRef.current.clear();
    historyImageAssetsRef.current.clear();
    bumpInlineImageRevision();
    bumpHistoryImageRevision();
    dispatch({ type: "reset" });
    if (pendingNotificationErrorRef.current) {
      dispatch({
        type: "command_error",
        detail: pendingNotificationErrorRef.current,
      });
      pendingNotificationErrorRef.current = null;
    }
    void import("./cache").then((module) => module.clearCache());
    void historyPageCacheRef.current.clear();
  }, [clearForkFocusLease, clearHistoryDetailRequests, machineId]);

  // The focused session's runtime (turns/state/model/perm/queue/...). Falls back
  // to an empty runtime before any session is focused.
  const focusedSid = state.focusedSid;
  const visibleParentSid = state.newChat ? null : focusedSid;
  const completionBadgeSids = new Set([
    ...Object.keys(completionReceipts),
    ...state.sessions.filter(
      (session) => session.completion_unread === true,
    ).map((session) => session.session_id),
    ...Object.keys(state.runtimes).filter(
      (sid) => state.runtimes[sid]?.completion?.unread === true),
  ]);
  const catalogCompletionBySid = new Map(state.sessions.flatMap((session) => {
    const completion = catalogCompletionProjection(session);
    return completion
      ? [[session.session_id, completion] as const]
      : [];
  }));
  const completionBadges = Object.fromEntries(
    [...completionBadgeSids].flatMap((sid) => {
      const local = completionReceipts[sid] ?? {
        main: false, mainCompletionId: null,
        mainTurnEndSeq: null, mainTurnEndGeneration: null, btwSids: [],
      };
      const authoritative = newestCompletionProjection(
        state.runtimes[sid]?.completion,
        catalogCompletionBySid.get(sid),
      );
      const kind = completionBadgeKind(local, authoritative?.unread);
      return kind ? [[sid, kind]] : [];
    }),
  ) as Record<string, CompletionBadgeKind>;
  const activeScopeKey = sessionScopeKey(machineId, engine, space);
  const currentCwd = state.sessions.find(session => session.session_id === focusedSid)?.cwd
    || state.cwdByScope[activeScopeKey] || "";
  const newChatCwd = state.newChat?.cwd ?? null;
  const newChatCatalogScopeKey = modelCatalogScopeKey(engine);
  const newChatCatalog = state.catalog;
  const focusedCatalog = state.catalog;
  const newChatDefaults = resolveNewChatLocalDefaults(
    engine,
    state.catalogDefault,
    state.catalogDefaultEffort,
    newChatCatalogScopeKey,
  );
  const rt = state.runtimes[focusedSid ?? ""] ?? createRuntime();
  // The card clears only when the wrapper closes the prompt, so release the
  // in-flight flag at the same moment (and when the user moves to another
  // session, so a later card is never born disabled).
  useEffect(() => {
    if (askAnswerPending && rt.pendingQuestion?.ask_id !== askAnswerPending) {
      setAskAnswerPending(null);
    }
  }, [askAnswerPending, rt.pendingQuestion]);
  useEffect(() => {
    if (!askCancelPending) return;
    if (rt.pendingQuestion?.ask_id !== askCancelPending) {
      setAskCancelPending(null);
      return;
    }
    // Re-enable retry if a stopped connection has not delivered the close.
    const timer = window.setTimeout(() => setAskCancelPending(null), 10_000);
    return () => window.clearTimeout(timer);
  }, [askCancelPending, rt.pendingQuestion]);
  const historyView = displayHistoryProjection(
    state.historyRecovery,
    focusedSid,
    rt,
    state.historyBrowse,
    state.retainedHistoryBrowse,
  );
  const focusedSession = state.sessions.find(
    (session) => session.session_id === focusedSid);
  const focusedEngine: Engine = focusedSession?.engine ?? engine;
  const focusedDshPresetLabel = focusedEngine === "dsh" && !state.newChat
    ? dshHeaderPresetLabel(
      focusedSession?.dsh_agent_preset,
      state.dshPresets,
      state.defaultDshPresetId,
    )
    : null;
  const capabilityCwd = focusedSession?.cwd || currentCwd;
  const focusedComposerDraftKey = composerDraftKey(
    machineId, space, focusedEngine, focusedSid ?? "",
  );
  const focusedSkillCatalogKey = skillCatalogKey(
    machineId, focusedEngine, space, capabilityCwd,
    focusedEngine === "dsh" ? focusedSid : null);
  focusedSkillScopeRef.current = {
    key: focusedSkillCatalogKey,
    engine: focusedEngine,
    space,
    cwd: capabilityCwd,
    skillsOnly: true,
    dshSessionId: focusedEngine === "dsh" ? focusedSid : null,
  };
  const inlineImageAssets = focusedSid
    ? inlineImageAssetsRef.current.forSession(focusedSid) : {};
  const historyImageAssets = focusedSid
    ? historyImageAssetsRef.current.forSession(focusedSid) : {};
  const unconfirmedQueued = collectUnconfirmedQueries(state.runtimes);
  const unconfirmedReplaceable = collectUnconfirmedQueries(
    state.runtimes, focusedSid);
  const queueCapacity = deferredQueueCapacity(state);
  const replaceQueueCapacity = deferredQueueCapacity(state, focusedSid);

  useEffect(() => {
    setQueuedQueryEditor((current) => (
      current
      && current.sid !== focusedSid
        ? null
        : current
    ));
  }, [focusedSid]);

  useEffect(() => {
    const acknowledgeVisible = () => {
      if (document.hidden) return;
      const current = stateRef.current;
      const parentSid = current.newChat ? null : current.focusedSid;
      if (!parentSid) return;
      const completion = newestCompletionProjection(
        current.runtimes[parentSid]?.completion,
        catalogCompletionProjection(current.sessions.find(
          (session) => session.session_id === parentSid)),
      );
      const completionId = completionAcknowledgementId(
        completionReceiptsRef.current[parentSid], completion);
      const mainAcknowledgementQueued = completionId !== null
        && wsRef.current?.sendAcknowledgeCompletionTo(
          parentSid, completionId) !== null;
      setCompletionReceipts((receipts) => acknowledgeCompletion(
        receipts, parentSid, { main: mainAcknowledgementQueued }));
    };
    acknowledgeVisible();
    document.addEventListener("visibilitychange", acknowledgeVisible);
    return () => document.removeEventListener(
      "visibilitychange", acknowledgeVisible);
  }, [
    visibleParentSid,
    rt.completion?.id,
    rt.completion?.unread,
    focusedSession?.completion_id,
    focusedSession?.completion_revision,
    focusedSession?.completion_unread,
  ]);

  const storeSkillCatalog = useCallback((
    key: string,
    items: EngineCapabilityItem[],
  ) => {
    const next = cacheSkillCatalog(
      skillCatalogsRef.current, key, items);
    skillCatalogsRef.current = next;
    setSkillCatalogs(next);
  }, []);
  const requestSkillCatalog = useCallback((
    request: SkillCatalogRequest,
    force = false,
  ) => {
    const cached = skillCatalogsRef.current[request.key];
    if (request.skillsOnly && !force && skillCatalogFresh(cached)) return false;
    return skillCatalogRequestsRef.current?.request(request) ?? false;
  }, []);
  const acceptSkillCatalog = useCallback((msg: EngineCapabilities) => {
    const coordinator = skillCatalogRequestsRef.current;
    if (!coordinator) return;
    const accepted = coordinator.accept(msg);
    if (!accepted) return;
    const matchedScope = accepted.request;
    if (!accepted.superseded && skillCatalogRefreshSucceeded(msg)) {
      storeSkillCatalog(
        matchedScope.key,
        msg.items.filter((item) => item.kind === "skill"));
    }
    if (!accepted.superseded && !msg.skills_only) {
      setCapabilitiesByScope((current) => ({
        ...current,
        [matchedScope.key]: msg,
      }));
    }
    if (focusedSkillScopeRef.current?.key === matchedScope.key
        && !coordinator.hasPendingMutation(matchedScope.key)
        && !coordinator.hasPendingRead(matchedScope.key, false)) {
      setCapabilitiesLoading(false);
    }
  }, [storeSkillCatalog]);
  const trackCapabilityMutation = useCallback((
    requestId: string | null | undefined,
    request: SkillCatalogRequest,
  ) => {
    if (!skillCatalogRequestsRef.current?.trackMutation(requestId, request)) {
      setCapabilitiesLoading(false);
    }
  }, []);
  const loadMessageImage = useCallback((
    sid: string,
    path: string,
    stablePreviewId?: string,
  ): boolean => {
    const ws = wsRef.current;
    const current = stateRef.current;
    const focusedSid = current.newChat ? null : current.focusedSid;
    if (!ws || focusedSid !== sid) return false;
    const cache = inlineImageAssetsRef.current;
    const assetKey = stablePreviewId ?? path;
    if (cache.has(sid, path, assetKey)) return true;
    const previewId = stablePreviewId ?? uuid();
    const requestId = uuid();
    if (!cache.begin({
      sid, path, assetKey, previewId, requestId,
    })) return false;
    if (!ws.sendGetPreviewAsset(path, previewId, requestId, sid)) {
      cache.cancel(requestId);
      return false;
    }
    bumpInlineImageRevision();
    return true;
  }, []);
  const loadFocusedMessageImage = useCallback((
    path: string,
    stablePreviewId?: string,
  ) => (
    focusedSid
      ? loadMessageImage(focusedSid, path, stablePreviewId)
      : false
  ), [focusedSid, loadMessageImage]);
  const authorizeMessageImage = useCallback((
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ): boolean => {
    const cache = inlineImageAssetsRef.current;
    const request = cache.authorizationRequest(authorization);
    if (!request) return false;
    if (!wsRef.current?.sendAuthorizePreview(
      authorization.authorizationId,
      authorization.requestId,
      decision,
      request.sid,
    )) return false;
    if (!cache.markAuthorizationSubmitting(authorization)) return false;
    bumpInlineImageRevision();
    return true;
  }, []);
  const loadHistoryImage = useCallback((
    turnId: string,
    imageId: string,
    variant: "thumbnail" | "full",
  ): boolean => {
    const sid = stateRef.current.focusedSid;
    const ws = wsRef.current;
    if (!sid || !ws) return false;
    const revision = stateRef.current.runtimes[sid]?.historyRevision;
    if (!revision) return false;
    const cache = historyImageAssetsRef.current;
    if (cache.has(sid, turnId, imageId, variant)) return true;
    const requestId = uuid();
    if (!cache.begin({
      sid, turnId, imageId, variant, requestId, revision,
    })) return false;
    if (!ws.sendGetHistoryImage(
      sid, turnId, imageId, variant, requestId, revision,
    )) {
      cache.cancel(requestId);
      return false;
    }
    bumpHistoryImageRevision();
    return true;
  }, []);

  // HttpOnly cookies can't be inspected from JS. Ask the relay whether this
  // browser session is still registered before opening a WebSocket; this also
  // makes relay restarts (which intentionally revoke old sessions) fail closed.
  useEffect(() => {
    // Never retain credentials/markers from the pre-HttpOnly implementation.
    clearLegacyAuthMarkers(localStorage);
    let cancelled = false;
    let timer: number | null = null;
    let backoff = 1000;
    let revealed = false;
    const check = async () => {
      const result = await probeSession();
      if (cancelled) return;
      if (result === "unavailable") {
        if (!revealed) {
          // A slow tunnel (frp + TLS) can push /api/session past the probe
          // timeout. Do not block the whole app on "正在连接中继": reveal the
          // login form after the first failed probe and keep re-probing in the
          // background so a valid session still resumes the moment the relay
          // answers.
          revealed = true;
          setAuthReady(true);
          setAuthed(false);
        }
        timer = window.setTimeout(check, backoff);
        backoff = Math.min(backoff * 2, 5000);
        return;
      }
      if (result === "unauthorized") {
        clearLegacyAuthMarkers(localStorage);
        // Do not expose the login form until prior-session prompts and
        // attachments are gone. A fast login must not race cache hydration.
        try { await import("./cache").then((module) => module.clearCache()); }
        catch { /* best-effort local cleanup */ }
        if (cancelled) return;
      }
      setAuthed(result === "authenticated");
      setAuthReady(true);
    };
    void check();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    if (!authed) {
      setRemoteDevices([]);
      setDevicesLoadState("idle");
      return;
    }
    setDevicesLoadState("loading");
    let cancelled = false;
    void fetch("/api/devices", {
      credentials: "same-origin", cache: "no-store",
    }).then(async (response) => response.ok ? response.json() : null)
      .then((payload) => {
        if (cancelled) return;
        if (!payload || !Array.isArray(payload.devices)) {
          setDevicesLoadState("error");
          return;
        }
        const validDevices: RemoteDevice[] = (payload.devices as unknown[]).filter((value: unknown): value is RemoteDevice => {
          if (!value || typeof value !== "object") return false;
          const item = value as Partial<RemoteDevice>;
          return typeof item.machine_id === "string"
            && /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/.test(item.machine_id)
            && typeof item.label === "string" && typeof item.online === "boolean";
        });
        const available = validDevices.map((device) => device.machine_id);
        setRemoteDevices(validDevices);
        setDevicesLoadState("ready");
        setDevicePairing(payload.pairing ?? { enabled: false, expires_at: null });
        if (available.length && !available.includes(machineId)) {
          setMachineId(available[0]);
        }
      }).catch(() => {
        if (!cancelled) setDevicesLoadState("error");
      });
    return () => { cancelled = true; };
  }, [authed, machineId]);

  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    const onMessage = (event: MessageEvent) => {
      if (event.data?.type !== "cc-remote-notification") return;
      const target = parseNotificationRoute(event.data.route);
      if (!target) return;
      notificationListRequestRef.current = null;
      notificationOriginRef.current = null;
      try { storeNotificationTarget(sessionStorage, target); }
      catch { /* retain the in-memory route below */ }
      setPendingNotificationTarget(target);
    };
    navigator.serviceWorker.addEventListener("message", onMessage);
    return () => navigator.serviceWorker.removeEventListener("message", onMessage);
  }, []);

  useEffect(() => pushCoordinatorRef.current?.subscribe(setPushBinding), []);

  useEffect(() => {
    const enabled = notificationMode !== "off"
      && typeof Notification !== "undefined"
      && Notification.permission === "granted";
    void pushCoordinatorRef.current?.setTarget(
      authed && enabled
        ? { machineId, mode: notificationMode }
        : null,
    );
  }, [authed, machineId, notificationMode]);

  useEffect(() => {
    document.documentElement.setAttribute("data-chat-spacing", chatSpacing);
    try { localStorage.setItem("cc_remote_chat_spacing", chatSpacing); } catch { /* private storage */ }
  }, [chatSpacing]);
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);
  useEffect(() => {
    document.documentElement.setAttribute("data-appearance", appearance);
    document.documentElement.setAttribute("data-layout", appearanceLayout(appearance));
    try { localStorage.setItem(APPEARANCE_KEY, appearance); } catch { /* private storage */ }
    const frame = requestAnimationFrame(() => {
      const color = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
      document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]').forEach(meta => { meta.content = color; });
    });
    return () => cancelAnimationFrame(frame);
  }, [appearance, theme]);
  const toggleTheme = () => setTheme((t) => (t === "dark" ? "light" : "dark"));

  // `engine` selects new-chat defaults. Both agents share the sidebar.
  const engineRef = useRef(engine);
  engineRef.current = engine;
  const spaceRef = useRef(space);
  spaceRef.current = space;
  const historyPageScopeFor = useCallback((
    sid: string,
    revision: string,
    fallbackEngine: Engine = engineRef.current,
    fallbackSpace: Space = spaceRef.current,
  ): HistoryPageCacheScope => {
    const listed = stateRef.current.sessions.find(
      (session) => session.session_id === sid)
      ?? Object.values(historySessionListsRef.current)
        .flat()
        .find((session) => session.session_id === sid);
    const scope: HistoryPageCacheScope = {
      machineId,
      engine: (listed?.engine === "dsh" || listed?.engine === "codex")
        ? listed.engine : fallbackEngine,
      space: listed?.space === "work" || listed?.space === "code"
        ? listed.space : fallbackSpace,
      sid,
      revision,
    };
    historyPageScopesRef.current.set(historyPageCacheScopeKey(scope), scope);
    return scope;
  }, [machineId]);
  const invalidateHistoryPageScopes = useCallback((sid: string) => {
    const scopes = new Map<string, HistoryPageCacheSessionScope>();
    for (const scope of historyPageScopesRef.current.values()) {
      if (scope.machineId !== machineId || scope.sid !== sid) continue;
      const sessionScope: HistoryPageCacheSessionScope = {
        machineId: scope.machineId,
        engine: scope.engine,
        space: scope.space,
        sid: scope.sid,
      };
      scopes.set(JSON.stringify(sessionScope), sessionScope);
    }
    for (const [surfaceKey, sessions] of Object.entries(
      historySessionListsRef.current)) {
      if (!sessions.some((session) => session.session_id === sid)) continue;
      const [listedSpace, listedEngine] = surfaceKey.split(":");
      if ((listedEngine !== "dsh" && listedEngine !== "codex")
          || (listedSpace !== "code" && listedSpace !== "work")) continue;
      if (listedEngine === "dsh" && listedSpace !== "code") continue;
      const sessionScope: HistoryPageCacheSessionScope = {
        machineId,
        engine: listedEngine,
        space: listedSpace,
        sid,
      };
      scopes.set(JSON.stringify(sessionScope), sessionScope);
    }
    if (stateRef.current.focusedSid === sid) {
      const sessionScope: HistoryPageCacheSessionScope = {
        machineId,
        engine: engineRef.current,
        space: spaceRef.current,
        sid,
      };
      scopes.set(JSON.stringify(sessionScope), sessionScope);
    }
    for (const scope of scopes.values()) {
      void historyPageCacheRef.current.invalidateScope(scope);
    }
    for (const [key, scope] of historyPageScopesRef.current) {
      if (scope.machineId === machineId && scope.sid === sid) {
        historyPageScopesRef.current.delete(key);
      }
    }
  }, [machineId]);
  const installBrowseHistoryPage = useCallback((
    history: History,
    waiters: readonly HistoryBrowseRequestContext[],
  ) => {
    const turns = summaryHistoryTurns(history);
    if (!history.before || !turns) return;
    for (const waiter of waiters) {
      const browse = stateRef.current.historyBrowse;
      if (!browse
          || stateRef.current.focusedSid !== history.session_id
          || browse.sid !== history.session_id
          || browse.scopeKey !== waiter.scopeKey
          || browse.revision !== history.revision
          || browse.viewId !== waiter.viewId
          || browse.windowEpoch !== waiter.windowEpoch
          || browse.olderCursor !== waiter.pendingBefore
          || waiter.pendingBefore !== history.before
          || (history.generation != null
            && browse.generation !== history.generation)) continue;
      const scope = historyPageScopesRef.current.get(waiter.scopeKey);
      if (!scope) continue;
      const page: HistoryBrowsePage = {
        pageKey: historyPageKey(history.before),
        turns,
        hasOlder: history.has_more,
        olderCursor: history.oldest_id
          ?? (turns[0] ? canonicalTurnId(turns[0]) : null),
        hasNewer: !!waiter.sourcePageKey,
        newerPageKey: waiter.sourcePageKey,
        isLatest: false,
      };
      const protectedTurnIds = protectedHistoryTurnIds(
        waiter.anchorTurnId,
        textSelectionGuardRef.current,
        {
          sid: history.session_id,
          revision: history.revision,
          viewId: waiter.viewId,
          scopeKey: waiter.scopeKey,
        },
      );
      const mutation = prependOlderPage(browse, page, {
        expectedScopeKey: waiter.scopeKey,
        expectedViewId: waiter.viewId,
        expectedWindowEpoch: waiter.windowEpoch,
        expectedOlderCursor: waiter.pendingBefore,
        protectedTurnIds,
      });
      if (mutation.projection === browse) continue;
      // Paint the received page immediately. Page-cache writes are best-effort
      // acceleration and must never hold the visible history response behind
      // IndexedDB quota/LRU work. A later cache miss keeps this readable window
      // mounted and degrades the downward affordance to "return to latest".
      void historyPageCacheRef.current.putPage(scope, page);
      for (const evicted of mutation.evictedPages) {
        void historyPageCacheRef.current.putPage(scope, evicted);
      }
      dispatch({
        type: "install_history_browse_page",
        sid: history.session_id,
        scopeKey: waiter.scopeKey,
        revision: history.revision,
        generation: history.generation,
        viewId: waiter.viewId,
        windowEpoch: waiter.windowEpoch,
        before: waiter.pendingBefore,
        page,
        protectedTurnIds,
        prepared: {
          from: browse,
          to: mutation.projection,
        },
      });
    }
  }, []);
  useEffect(() => {
    document.documentElement.setAttribute("data-engine", engine);
    localStorage.setItem(ENGINE_KEY, engine);
    wsRef.current?.setSurface(engine, space);
    wsRef.current?.sendListSessions(engine, space);
  }, [engine, space]);
  useEffect(() => {
    document.documentElement.setAttribute("data-space", space);
    localStorage.setItem(LEGACY_SPACE_KEY, space);
    spacesByEngineRef.current = rememberEngineSpace(
      spacesByEngineRef.current, engine, space);
    localStorage.setItem(ENGINE_SPACES_KEY, JSON.stringify(spacesByEngineRef.current));
  }, [engine, space]);
  useEffect(() => {
    if (newChatCwd === null || state.connState !== "connected"
        || !state.wrapperOnline) return;
    const request = newChatCatalogRequest(engine);
    wsRef.current?.sendGetModels(request.engine, request.cwd);
  }, [
    engine,
    newChatCwd,
    space,
    state.connState,
    state.wrapperOnline,
  ]);
  useEffect(() => {
    if (!state.newChat) return;
    const reconciled = reconcileNewChatSelection(
      engine,
      state.newChat.model,
      state.newChat.effort,
      newChatCatalog,
      newChatDefaults.model,
    );
    if (reconciled.model === state.newChat.model
        && reconciled.effort === state.newChat.effort) return;
    dispatch({ type: "set_new_chat_selection", ...reconciled });
  }, [
    engine,
    newChatCatalog,
    newChatDefaults.model,
    state.newChat,
  ]);
  useEffect(() => {
    if (state.newChat || state.connState !== "connected"
        || !state.wrapperOnline) return;
    if (focusedSid) wsRef.current?.sendGetModels(focusedEngine, undefined, focusedSid);
  }, [
    focusedEngine,
    focusedSid,
    state.connState,
    state.newChat,
    state.wrapperOnline,
  ]);
  useEffect(() => {
    // TurnDetail has no request id. Any view navigation revokes the frozen
    // runtime/browse target so a delayed response cannot cross sessions.
    clearHistoryDetailRequests();
  }, [
    clearHistoryDetailRequests,
    machineId, engine, space, focusedSid, state.newChat,
  ]);
  const rememberSurfaceFocus = useCallback((
    currentEngine: Engine,
    currentSpace: Space,
  ) => {
    if (focusedSid && !state.newChat) {
      lastFocusBySurfaceRef.current[
        sessionScopeKey(machineId, currentEngine, currentSpace)
      ] = focusedSid;
    }
  }, [focusedSid, machineId, state.newChat]);

  const prepareSurfaceSwitch = useCallback((
    nextEngine: Engine,
    nextSpace: Space,
    preserveAuthority = false,
  ) => {
    clearForkFocusLease(false);
    createdSessionFocusRef.current = null;
    rememberSurfaceFocus(engine, space);
    const surfaceKey = `${nextSpace}:${nextEngine}`;
    const focusScopeKey = sessionScopeKey(
      machineId, nextEngine, nextSpace);
    if (!preserveAuthority) {
      authoritativeSurfaceListsRef.current.delete(surfaceKey);
    }
    dispatch({
      type: "restore_session_list",
      sessions: mergedAgentSessions(sessionListsBySurfaceRef.current),
    });
    const remembered = lastFocusBySurfaceRef.current[focusScopeKey];
    preferredSurfaceFocusRef.current = preserveAuthority
      ? null
      : remembered ? { key: focusScopeKey, sid: remembered } : null;
    didInitFocusRef.current = preserveAuthority;
    wsRef.current?.setSurface(nextEngine, nextSpace);
    wsRef.current?.setFocusedSid(null);
    // Keep the previous surface's transcript out of view while its accepted
    // list is restored. The focus effect below exits this temporary new page as
    // soon as the remembered (or latest valid) session is available.
    const current = stateRef.current;
    dispatch({
      type: "enter_new_chat",
      cwd: "~",
      cwdSource: "default",
      dshAgentPreset: nextEngine === "dsh"
        ? current.defaultDshPresetId
        : null,
    });
    setNewChatAutoFocus(false);
  }, [clearForkFocusLease, engine, machineId, rememberSurfaceFocus, space]);

  const focusListedSession = useCallback((selected: SessionInfo) => {
    const editor = document.activeElement;
    if (editor instanceof HTMLTextAreaElement || editor instanceof HTMLInputElement) editor.blur();
    const selectedEngine: Engine = selected.engine === "dsh" || selected.engine === "codex"
      ? selected.engine : engineRef.current;
    const selectedSpace: Space = selected.space === "work"
      || selected.space === "code"
      ? selected.space : spaceRef.current;
    const id = selected.session_id;
    setEngine(selectedEngine);
    if (forkFocusLeaseRef.current?.childSessionId !== id) {
      clearForkFocusLease(false);
    }
    pendingCreateRef.current = null;
    createdSessionFocusRef.current = null;
    didInitFocusRef.current = true;
    setCreateError(null);
    dispatch({ type: "exit_new_chat" });
    dispatch({ type: "focus_session", sid: id });
    wsRef.current?.setFocusedSid(id, selectedEngine, selectedSpace);
    requestHistory(id, undefined, HISTORY_INITIAL_PAGE);
    wsRef.current?.sendSwitchSession(id, selectedEngine, selectedSpace);
    if (isMobile()) setSidebarOpen(false);
  }, [clearForkFocusLease, requestHistory]);

  useEffect(() => {
    const target = pendingNotificationTarget;
    if (!target || !authed) return;
    const surfaceKey = `${target.space}:${target.engine}`;
    if (notificationOriginRef.current === null) {
      notificationOriginRef.current = {
        machineId,
        engine,
        space,
        sid: state.focusedSid,
      };
      // A cached/preloaded catalog is paint-only here. Resolve every click
      // against a list requested after that click so a deleted or revoked
      // target cannot move focus.
      authoritativeSurfaceListsRef.current.delete(surfaceKey);
      notificationListRequestRef.current = null;
    }
    const navigation = resolveNotificationNavigation({
      target,
      origin: notificationOriginRef.current,
      deviceState: devicesLoadState,
      authorizedMachineIds: remoteDevices.map((device) => device.machine_id),
      machineId,
      engine,
      space,
      authoritativeSessions: authoritativeSurfaceListsRef.current.has(surfaceKey)
        ? sessionListsBySurfaceRef.current[surfaceKey] ?? []
        : null,
    });
    if (navigation.kind === "wait") return;
    if (navigation.kind === "fail") {
      const detail = navigation.reason === "devices_unavailable"
        ? "无法验证链接对应的设备，当前会话没有切换。"
        : navigation.reason === "device_missing"
          ? "链接对应的设备不存在或当前账号无权访问。"
          : "链接对应的会话已删除或不属于当前设备。";
      if (navigation.restore) {
        const origin = navigation.restore;
        pendingNotificationErrorRef.current = detail;
        setPendingNotificationTarget(null);
        notificationListRequestRef.current = null;
        notificationOriginRef.current = null;
        try { sessionStorage.removeItem(NOTIFICATION_TARGET_KEY); }
        catch { /* best-effort stale route cleanup */ }
        preferredSurfaceFocusRef.current = origin.sid
          ? {
              key: sessionScopeKey(
                origin.machineId, origin.engine, origin.space),
              sid: origin.sid,
            }
          : null;
        didInitFocusRef.current = false;
        setEngine(origin.engine);
        setSpace(origin.space);
        setMachineId(origin.machineId);
      } else {
        dispatch({ type: "command_error", detail });
        cancelPendingNotificationTarget();
      }
      return;
    }
    if (navigation.kind === "switch_machine") {
      setMachineId(navigation.machineId);
      return;
    }
    if (navigation.kind === "request_list") {
      const ws = wsRef.current;
      if (!ws) return;
      const requestKey = `${machineId}:${surfaceKey}`;
      if (notificationListRequestRef.current !== requestKey) {
        if (ws.sendListSessions(navigation.engine, navigation.space)) {
          notificationListRequestRef.current = requestKey;
        }
      }
      return;
    }
    if (navigation.kind === "switch_surface") {
      pendingCreateRef.current = null;
      setCreateError(null);
      prepareSurfaceSwitch(navigation.engine, navigation.space, true);
      setEngine(navigation.engine);
      setSpace(navigation.space);
      return;
    }
    didInitFocusRef.current = true;
    focusListedSession(navigation.session);
    cancelPendingNotificationTarget();
  }, [
    authed,
    cancelPendingNotificationTarget,
    devicesLoadState,
    engine,
    focusListedSession,
    machineId,
    notificationListRevision,
    pendingNotificationTarget,
    prepareSurfaceSwitch,
    remoteDevices,
    space,
    state.focusedSid,
  ]);

  // Engine and Work/Code switches are navigation. Each surface restores the
  // session that was last open there instead of silently starting a new one.
  const switchSpace = (next: Space) => {
    if (engine === "dsh" && next !== "code") return;
    if (next === space || !confirmArtifactDiscard()) return;
    cancelPendingNotificationTarget();
    pendingCreateRef.current = null;
    setCreateError(null);
    prepareSurfaceSwitch(engine, next);
    spaceRef.current = next;
    setSpace(next);
  };

  // WebSocket lifecycle
  useEffect(() => {
    if (!authed) return;
    const historyRequests = historyRequestsRef.current;
    const lifecycleEpoch = ++wsLifecycleEpochRef.current;
    didInitFocusRef.current = false;  // re-arm initial-focus for this connection lifecycle
    authoritativeSurfaceListsRef.current.delete(`${spaceRef.current}:${engineRef.current}`);

    let cancelled = false;
    let effectWs: RelayWs | null = null;
    const acceptsLifecycle = () => !cancelled
      && wsLifecycleEpochRef.current === lifecycleEpoch;
    const recoverableReads = new RecoverableReadCoordinator(
      (callback, delayMs) => window.setTimeout(callback, delayMs),
      (timer) => window.clearTimeout(timer),
    );
    // A snapshot announces a session (cc_session_id/state/cwd). We do NOT reset
    // the cursor here anymore — cursors are seeded from the IndexedDB cache before
    // connecting, so hello asks the wrapper only for the DELTA instead of a full
    // history replay of every resident session (that flood wedged reconnect).
    function handleSnapshot(e: Snapshot, ownership?: EventOwnership) {
      dispatch({ type: "event", event: e, ownership });
    }

    (async () => {
      let seeded = { cursors: {} as Record<string, number>,
        generations: {} as Record<string, string>,
        controls: {} as Record<string, SessionControl> };
      let seedDeadline: number | undefined;
      try {
        // Storage is an accelerator, never a gate on the live connection.
        // Safari may park IDB while another tab upgrades/owns the database.
        seeded = await Promise.race([
          import("./cache").then((m) => m.loadAllReplayState()),
          new Promise<typeof seeded>((resolve) => {
            seedDeadline = window.setTimeout(() => resolve(seeded), 350);
          }),
        ]);
      } catch { /* best-effort */ }
      finally { if (seedDeadline !== undefined) window.clearTimeout(seedDeadline); }
      if (cancelled) return;
      const ws = new RelayWs({
        onEvent: (msg, ownership) => {
          if (!acceptsLifecycle()) return;
          if (msg.type === "account_usage_report") {
            if (accountUsageRequests.current[msg.engine] === msg.request_id) {
              setAccountUsageReports(current => ({...current, [msg.engine]: msg}));
            }
            return;
          }
          if (msg.type === "files_list") { setFilesList(msg); return; }
          if (msg.type === "engine_catalog") {
            for (const item of msg.engines) {
              if ((item.id === "dsh" || item.id === "codex") && item.available) {
                ws.sendListSessions(item.id, "code");
              }
            }
          }
          // SessionList is a scoped response, not a broadcast catalog. Without
          // the exact request ownership it may belong to an older surface or
          // WebSocket generation and must not mutate any sidebar/focus cache.
          if (msg.type === "session_list" && !ownership) return;
          if (msg.type === "completion_state" && msg.sid
              && msg.unread !== true) {
            // Clear the page-local fallback as soon as the wrapper confirms an
            // acknowledgement for the same completion from any browser. A
            // delayed read receipt for an older completion cannot clear a
            // newer turn_end fallback.
            const authoritative = newestCompletionProjection(
              stateRef.current.runtimes[msg.sid]?.completion,
              {
                id: msg.completion_id ?? null,
                unread: msg.unread ?? false,
                revision: msg.revision ?? 0,
              },
            );
            setCompletionReceipts((receipts) => (
              acknowledgeMatchingCompletion(
                receipts, msg.sid!, authoritative,
                {
                  authoritativeSeq: msg.seq,
                  authoritativeGeneration: ws.generationFor(msg.sid!),
                })));
          } else if (msg.type === "session_list") {
            // A sleeping browser can miss the live acknowledgement and later
            // reconnect after the session was evicted. Its catalog row carries
            // the same durable receipt, so clear any older page-local fallback
            // instead of letting it reappear if the runtime is pruned.
            setCompletionReceipts((receipts) => {
              let next = receipts;
              for (const session of msg.sessions) {
                const catalog = catalogCompletionProjection(session);
                if (!catalog) continue;
                const authoritative = newestCompletionProjection(
                  stateRef.current.runtimes[session.session_id]?.completion,
                  catalog,
                );
                next = acknowledgeMatchingCompletion(
                  next, session.session_id, authoritative);
              }
              return next;
            });
          }
          if (msg.type === "session_rekey"
              && inlineImageAssetsRef.current.rekeySession(
                msg.old_key,
                msg.session_id,
              )) {
            bumpInlineImageRevision();
          }
          if (msg.type === "preview_authorization_required"
              && inlineImageAssetsRef.current.requireAuthorization(msg)) {
            bumpInlineImageRevision();
          }
          if (msg.type === "preview_authorization_result") {
            const retry =
              inlineImageAssetsRef.current.acceptAuthorizationResult(msg);
            if (retry !== false) {
              bumpInlineImageRevision();
              if (retry && !ws.sendGetPreviewAsset(
                retry.path,
                retry.previewId,
                retry.requestId,
                retry.sid,
              )) {
                inlineImageAssetsRef.current.cancel(retry.requestId);
                bumpInlineImageRevision();
              }
            }
          }
          if (msg.type === "preview_asset"
              && inlineImageAssetsRef.current.accept(msg)) {
            bumpInlineImageRevision();
          }
          if (msg.type === "history_image"
              && historyImageAssetsRef.current.accept(msg)) {
            bumpHistoryImageRevision();
          }
          if (msg.type === "queued_query_detail") {
            setQueuedQueryEditor((current) => (
              current
              && current.sid === msg.sid
              && current.msgId === msg.msg_id
              && current.detailRequestId === msg.request_id
                ? {
                    ...current,
                    prompt: msg.prompt ?? null,
                    kind: msg.kind ?? null,
                    images: msg.images ?? undefined,
                    files: msg.files ?? undefined,
                    imageCount: msg.image_count,
                    fileCount: msg.file_count,
                    loading: false,
                    error: msg.error ?? null,
                    detailRequestId: null,
                  }
                : current
            ));
            return;
          }
          if (msg.type === "queued_query_updated") {
            setQueuedQueryEditor((current) => {
              if (!current
                  || current.sid !== msg.sid
                  || current.msgId !== msg.msg_id
                  || current.updateRequestId !== msg.request_id) {
                return current;
              }
              if (!msg.updated) {
                return {
                  ...current,
                  saving: false,
                  error: msg.error ?? "排队消息修改失败。",
                  updateRequestId: null,
                  pendingPrompt: null,
                };
              }
              return null;
            });
            return;
          }
          if (msg.type === "query_queue" && msg.sid) {
            setQueuedQueryEditor((current) => {
              if (!current || current.sid !== msg.sid) return current;
              return msg.items.some(
                (item) => item.msg_id === current.msgId) ? current : null;
            });
          }
          if (msg.type === "session_rekey") {
            setQueuedQueryEditor((current) => (
              current?.sid === msg.old_key
                ? { ...current, sid: msg.session_id }
                : current
            ));
          }
          if (msg.type === "turn_end" && msg.sid && !msg.result.is_error) {
            const current = stateRef.current;
            const sameVisibleParent = !document.hidden
              && !current.newChat
              && current.focusedSid === msg.sid;
            if (!sameVisibleParent) {
              setCompletionReceipts((receipts) => markCompletionUnread(
                receipts,
                msg.sid!,
                msg.sid!,
                "main",
                msg.turn_id ?? null,
                msg.seq ?? null,
                ws.generationFor(msg.sid!),
              ));
            }
          }
          if ((msg.type === "user_msg" || msg.type === "turn_steered"
                || msg.type === "turn_end") && msg.sid) {
            const activityMs = Math.round(msg.ts * 1000);
            let changed = false;
            for (const [key, listed] of Object.entries(
              sessionListsBySurfaceRef.current)) {
              const updated = bumpSessionActivity(listed, msg.sid, activityMs);
              if (updated !== listed) {
                sessionListsBySurfaceRef.current[key] = updated;
                changed = true;
              }
            }
            if ((msg.type === "user_msg" || msg.type === "turn_steered")
                && changed) {
              sessionActivityPendingRef.current.add(msg.sid);
            }
          }
          if (msg.type === "history_invalidated") {
            const sid = msg.session_id;
            clearHistoryDetailRequests();
            invalidateHistoryPageScopes(sid);
            if (inlineImageAssetsRef.current.dropSession(sid)) {
              bumpInlineImageRevision();
            }
            if (historyImageAssetsRef.current.dropSession(sid)) {
              bumpHistoryImageRevision();
            }
            if (historyInvalidationsRef.current.get(sid) !== msg.revision) {
              historyInvalidationsRef.current.set(sid, msg.revision);
              if (!historyInvalidationGenerationsRef.current.has(sid)) {
                historyInvalidationGenerationsRef.current.set(sid, null);
              }
              historyCacheEpochRef.current.set(
                sid, (historyCacheEpochRef.current.get(sid) ?? 0) + 1);
              void import("./cache").then((module) =>
                module.invalidateSessionCache(sid));
            }
            // The marker is deliberately tiny/replayable; the full replacement
            // is one-shot and may have been dropped by a disconnect or frame
            // size bound. Fetch immediately when visible; a background session
            // gets the same authoritative request when it is later focused.
            if (stateRef.current.focusedSid === sid) {
              requestHistory(
                sid, undefined, HISTORY_INITIAL_PAGE, undefined, msg.revision);
            }
          } else if (msg.type === "replay_start" && msg.sid
              && (msg.truncated || msg.rebuild)) {
            const sid = msg.sid;
            clearHistoryDetailRequests();
            invalidateHistoryPageScopes(sid);
            historyInvalidationGenerationsRef.current.set(
              sid, msg.generation ?? null);
            // If a marker is still retained inside this replay it will follow
            // ReplayStart and replace null with its exact revision.
            if (!historyInvalidationsRef.current.has(sid)) {
              historyInvalidationsRef.current.set(sid, null);
              historyCacheEpochRef.current.set(
                sid, (historyCacheEpochRef.current.get(sid) ?? 0) + 1);
              void import("./cache").then((module) =>
                module.invalidateSessionCache(sid));
            }
            if (stateRef.current.focusedSid === sid) {
              requestHistory(
                sid, undefined, HISTORY_INITIAL_PAGE, msg.generation);
            }
          } else if (msg.type === "history" && msg.authoritative !== false && !msg.before
              && historyInvalidationsRef.current.has(msg.session_id)) {
            const expected = historyInvalidationsRef.current.get(msg.session_id);
            const expectedGeneration =
              historyInvalidationGenerationsRef.current.get(msg.session_id) ?? null;
            const recovery = stateRef.current.historyRecovery;
            const currentRuntime =
              stateRef.current.runtimes[msg.session_id];
            const displayRecoveryMatches = !isHistoryRecoveryPending(
              recovery, msg.session_id)
              || historyConfirmsRecovery(recovery, msg);
            const runtimeRecoveryMatches =
              !isRuntimeHistoryRecoveryPending(currentRuntime)
              || historyConfirmsRuntimeRecovery(currentRuntime, msg);
            const sameBuildGeneration = msg.generation != null
              ? currentRuntime?.historyGeneration === msg.generation
              : currentRuntime?.historyGeneration == null
                && currentRuntime?.historyRevision === msg.revision;
            const buildIsCurrent = msg.build_seq == null
              || !sameBuildGeneration
              || msg.build_seq >= (currentRuntime?.historyBuildSeq ?? 0);
            if ((expected === null || expected === msg.revision)
                && (expectedGeneration === null
                  || expectedGeneration === msg.generation)
                && displayRecoveryMatches
                && runtimeRecoveryMatches
                && buildIsCurrent) {
              // A late first page from an older revision must not re-enable the
              // cache behind a newer destructive marker.
              historyInvalidationsRef.current.delete(msg.session_id);
              historyInvalidationGenerationsRef.current.delete(msg.session_id);
              void import("./cache").then((module) =>
                module.allowSessionCache(msg.session_id));
            }
          }
          if (msg.type === "history") {
            const completedHistory =
              historyRequestsRef.current.complete(msg);
            const browseWaiters = completedHistory.matched;
            for (const browse of completedHistory.stale) {
              dispatch({
                type: "history_browse_page_failed",
                sid: msg.session_id,
                scopeKey: browse.scopeKey,
                revision: stateRef.current.historyBrowse?.revision
                  ?? msg.revision,
                generation: stateRef.current.historyBrowse?.generation,
                viewId: browse.viewId,
                windowEpoch: browse.windowEpoch,
                before: browse.pendingBefore,
              });
            }
            const retryKey = ["history", msg.session_id, msg.before ?? "",
              msg.revision ?? ""].join("\u0000");
            let retryScheduled = false;
            if (msg.authoritative === false) {
              const retryDelay = msg.error == null && !msg.before
                ? HISTORY_PROVISIONAL_WATCHDOG_MS
                : undefined;
              retryScheduled = recoverableReads.retry(retryKey, () => {
                if (cancelled) return;
                if (stateRef.current.focusedSid !== msg.session_id) return;
                if (msg.before) {
                  for (const browse of browseWaiters) {
                    requestHistory(
                      msg.session_id, msg.before, HISTORY_MORE_PAGE,
                      msg.generation, msg.revision, browse);
                  }
                } else {
                  requestHistory(
                    msg.session_id, undefined, HISTORY_INITIAL_PAGE,
                    msg.generation, msg.revision);
                }
              }, retryDelay);
            } else {
              recoverableReads.complete(retryKey);
            }
            if (msg.before) {
              if (completedHistory.stale.length > 0
                  && stateRef.current.focusedSid === msg.session_id) {
                // The cursor came from an obsolete revision/generation. Exit
                // that read-only browse lifetime and refresh the canonical head
                // rather than leaving mobile paging in a permanent spinner.
                dispatch({ type: "return_to_latest", sid: msg.session_id });
                const currentRuntime =
                  stateRef.current.runtimes[msg.session_id];
                const currentGeneration = ws.generationFor(msg.session_id)
                  ?? currentRuntime?.pendingHistoryGeneration
                  ?? currentRuntime?.historyGeneration;
                const currentRevision = currentRuntime?.pendingHistoryRevision
                  ?? (currentRuntime?.historyInvalidated
                    ? undefined : currentRuntime?.historyRevision);
                requestHistory(
                  msg.session_id, undefined, HISTORY_INITIAL_PAGE,
                  currentGeneration, currentRevision);
              }
              if (msg.authoritative !== false) {
                void installBrowseHistoryPage(msg, browseWaiters);
              } else if (!retryScheduled) {
                for (const browse of browseWaiters) {
                  dispatch({
                    type: "history_browse_page_failed",
                    sid: msg.session_id,
                    scopeKey: browse.scopeKey,
                    revision: msg.revision,
                    generation: msg.generation,
                    viewId: browse.viewId,
                    windowEpoch: browse.windowEpoch,
                    before: browse.pendingBefore,
                  });
                }
              }
              // History.before is display-only and requester-correlated. It
              // must never fall through to the generic runtime reducer.
              return;
            }
            const needsRecoveryConfirmation =
              historyNeedsConfirmationRequest(
                stateRef.current.runtimes[msg.session_id], msg);
            if (needsRecoveryConfirmation
                && stateRef.current.focusedSid === msg.session_id
                && msg.generation) {
              requestHistory(
                msg.session_id, undefined, HISTORY_INITIAL_PAGE,
                msg.generation);
            }
          }
          if (msg.type === "turn_detail") {
            const detailTargets =
              historyDetailRequestsRef.current.completeAll(msg);
            if (detailTargets.length === 0) return;
            const retryKey = [
              "detail", msg.session_id, msg.revision, msg.turn_id,
              msg.before ?? "",
            ].join("\u0000");
            if (msg.authoritative === false) {
              let runtimeReleased = false;
              for (const detailTarget of detailTargets) {
                if (detailTarget.target === "browse") {
                  dispatch({
                    type: "history_browse_detail",
                    sid: detailTarget.sid,
                    scopeKey: detailTarget.scopeKey,
                    revision: detailTarget.revision,
                    viewId: detailTarget.viewId,
                    windowEpoch: detailTarget.windowEpoch,
                    turnId: detailTarget.turnId,
                    events: [],
                    error: msg.error,
                    before: detailTarget.before,
                  });
                } else if (!runtimeReleased) {
                  dispatch({ type: "event", event: msg, ownership });
                  runtimeReleased = true;
                }
              }
              recoverableReads.retry(retryKey, () => {
                if (cancelled) return;
                if (stateRef.current.focusedSid !== msg.session_id) return;
                const current = stateRef.current;
                const activeTargets = detailTargets.filter((detailTarget) => {
                  if (detailTarget.target === "browse") {
                    const browse = current.historyBrowse;
                    return !!browse
                      && browse.scopeKey === detailTarget.scopeKey
                      && browse.viewId === detailTarget.viewId
                      && browse.revision === detailTarget.revision
                      && browse.turns.some((turn) =>
                        canonicalTurnId(turn) === detailTarget.turnId
                        || turn.id === detailTarget.turnId);
                  }
                  const runtime = current.runtimes[msg.session_id];
                  const turn = runtime?.turns.find(
                    (item) => canonicalTurnId(item) === msg.turn_id
                      || item.id === msg.turn_id);
                  return !!turn && (!!detailTarget.before || !turn.detailLoaded)
                    && runtime?.historyRevision === detailTarget.revision;
                });
                if (activeTargets.length === 0) return;
                const registrations = activeTargets.map((detailTarget) => ({
                  detailTarget,
                  registration:
                    historyDetailRequestsRef.current.register(detailTarget),
                })).filter(({ registration }) => registration.accepted);
                if (registrations.length === 0) return;
                if (registrations.some(({ registration }) => registration.send)) {
                  const target = registrations[0].detailTarget;
                  const sent = ws.sendGetTurnDetail(
                    msg.session_id, msg.turn_id, target.revision, target.before);
                  if (!sent) {
                    for (const { detailTarget } of registrations) {
                      historyDetailRequestsRef.current.cancel(detailTarget);
                    }
                    return;
                  }
                }
                for (const { detailTarget } of registrations) {
                  if (detailTarget.target === "browse") {
                    dispatch({
                      type: "history_browse_detail_requested",
                      sid: detailTarget.sid,
                      scopeKey: detailTarget.scopeKey,
                      revision: detailTarget.revision,
                      viewId: detailTarget.viewId,
                      windowEpoch: detailTarget.windowEpoch,
                      turnId: detailTarget.turnId,
                      before: detailTarget.before,
                    });
                  } else {
                    dispatch({
                      type: "turn_detail_requested", sid: detailTarget.sid,
                      turnId: detailTarget.turnId,
                      before: detailTarget.before,
                      autoLoad: detailTarget.autoLoad,
                    });
                  }
                }
              });
              return;
            } else {
              recoverableReads.complete(retryKey);
            }
            let runtimeTarget = false;
            for (const detailTarget of detailTargets) {
              if (detailTarget.target === "runtime") {
                runtimeTarget = true;
                continue;
              }
              dispatch({
                type: "history_browse_detail",
                sid: detailTarget.sid,
                scopeKey: detailTarget.scopeKey,
                revision: detailTarget.revision,
                viewId: detailTarget.viewId,
                windowEpoch: detailTarget.windowEpoch,
                turnId: detailTarget.turnId,
                events: msg.events,
                before: msg.before,
                hasMore: msg.has_more,
                oldestCursor: msg.oldest_cursor,
                hasNewer: msg.has_newer,
                newerCursor: msg.newer_cursor,
              });
            }
            if (!runtimeTarget) return;
          }
          if (msg.type === "session_forked") {
            const pendingMessageFork = pendingSessionForkRef.current;
            const matchesMessageFork = msg.target === "same_cwd"
              && matchesSessionForkRequest(
              pendingMessageFork, msg.request_id,
              msg.parent_session_id, msg.last_turn_id);
            if (!matchesMessageFork) return;
            const targetEngine = pendingMessageFork!.engine;
            pendingSessionForkRef.current = null;
            dispatch({ type: "exit_new_chat" });
            dispatch({ type: "focus_session", sid: msg.session_id });
            startForkFocusLease({
              requestId: msg.request_id,
              parentSessionId: msg.parent_session_id,
              childSessionId: msg.session_id,
              engine: targetEngine,
              space: "code",
              machineId,
              cwd: msg.cwd,
              gitBranch: msg.git_branch,
              refreshAt: Date.now() + FORK_FOCUS_REFRESH_MS,
            });
            ws.setSessionEngines([{
              session_id: msg.session_id,
              engine: targetEngine,
              space: "code",
            }]);
            ws.setFocusedSid(msg.session_id, targetEngine, "code");
            ws.sendListSessions(targetEngine, "code");
            requestHistory(
              msg.session_id, undefined, HISTORY_INITIAL_PAGE);
            ws.sendSwitchSession(msg.session_id, targetEngine, "code");
            if (isMobile()) setSidebarOpen(false);
            return;
          }
          if (msg.type === "error"
              && msg.code !== "wrapper_offline"
              && msg.code !== "fork_reconciling"
              && matchesSessionForkRequest(
                pendingSessionForkRef.current, msg.request_id)) {
            pendingSessionForkRef.current = null;
            dispatch({ type: "command_error", detail: presentCommandProblem(msg) });
            return;
          }
          const createResponseRequestId = (msg.type === "session_focus"
              || msg.type === "error") ? msg.request_id : null;
          const createRequest = createResponseRequestId
            ? createRequestsRef.current.get(createResponseRequestId) : undefined;
          if (createRequest && (msg.type === "session_focus"
              || (msg.type === "error" && msg.code !== "wrapper_offline"))) {
            createRequestsRef.current.delete(createResponseRequestId!);
            if (createResponseRequestId !== pendingCreateRef.current) return;
            pendingCreateRef.current = null;
            const currentScopeKey = sessionScopeKey(
              machineId, engineRef.current, spaceRef.current);
            if (createRequest.scopeKey !== currentScopeKey) return;
            if (msg.type === "session_focus") {
              didInitFocusRef.current = true;
              preferredSurfaceFocusRef.current = null;
              createdSessionFocusRef.current = {
                scopeKey: currentScopeKey, expiresAt: Date.now() + 60_000,
                session: { session_id: msg.session_id, cwd: msg.cwd, summary: "新会话",
                  engine: engineRef.current, space: spaceRef.current,
                  last_modified: String(msg.ts), state: "running" },
              };
              const surfaceKey = `${spaceRef.current}:${engineRef.current}`;
              const sessions = [createdSessionFocusRef.current.session,
                ...(sessionListsBySurfaceRef.current[surfaceKey] ?? [])
                  .filter(row => row.session_id !== msg.session_id)];
              sessionListsBySurfaceRef.current[surfaceKey] = sessions;
              historySessionListsRef.current[surfaceKey] = sessions;
              dispatch({ type: "restore_session_list",
                sessions: mergedAgentSessions(sessionListsBySurfaceRef.current) });
              composerDraftsRef.current.delete(createRequest.draftKey);
              composerDraftsRef.current.rekeyHistory(createRequest.draftKey,
                composerDraftKey(machineId, spaceRef.current, engineRef.current, msg.session_id));
              setCreateError(null);
              dispatch({ type: "exit_new_chat" });
            } else {
              if (msg.code === "invalid_cwd"
                  && createRequest.cwdSource === "inherited") {
                dispatch({
                  type: "clear_scope_cwd",
                  scopeKey: createRequest.scopeKey,
                });
                dispatch({
                  type: "set_new_chat_cwd",
                  cwd: "~",
                  cwdSource: "default",
                });
              }
              setCreateError(presentCommandProblem(msg));
              return;
            }
          }
          if (msg.type === "snapshot") {
            handleSnapshot(msg, ownership);
            return;
          }
          if (msg.type === "session_rekey") {
            for (const [key, rows] of Object.entries(sessionListsBySurfaceRef.current)) {
              if (!rows.some(row => row.session_id === msg.old_key)) continue;
              const sessions = rows.filter(row => row.session_id !== msg.session_id)
                .map(row => row.session_id === msg.old_key
                  ? { ...row, session_id: msg.session_id, cwd: msg.cwd } : row);
              sessionListsBySurfaceRef.current[key] = sessions;
              historySessionListsRef.current[key] = sessions;
            }
            const created = createdSessionFocusRef.current;
            if (created?.session.session_id === msg.old_key) {
              createdSessionFocusRef.current = { ...created, session: {
                ...created.session, session_id: msg.session_id, cwd: msg.cwd,
              } };
            }
            setCompletionReceipts((current) => rekeyCompletionReceipts(
              current, msg.old_key, msg.session_id));
            if (ownership) {
              composerDraftsRef.current.rekey(
                composerDraftKey(
                  ownership.machineId, ownership.space, ownership.engine,
                  msg.old_key,
                ),
                composerDraftKey(
                  ownership.machineId, ownership.space, ownership.engine,
                  msg.session_id,
                ),
              );
            }
            if (stateRef.current.focusedSid === msg.old_key
                && ownership?.engine === engineRef.current
                && ownership.space === spaceRef.current) {
              // The reducer has already got enough correlated metadata to paint
              // a temp sidebar row. Rekey is the durability boundary: refresh
              // the active surface so its title/status comes from the native
              // catalog without making the user toggle or reload the page.
              ws.sendListSessions(engineRef.current, spaceRef.current);
            }
          }
          // RelayWs only assigns ownership to a correlated, current-generation
          // catalog response. Reject an old/unknown list before it can rewrite
          // session maps or warm a sibling surface.
          if (msg.type === "session_list" && !ownership) return;
          let normalizedListedSessions: SessionInfo[] | null = null;
          if (msg.type === "session_list") {
            const listedSpace = msg.space ?? "code";
            const surfaceKey = `${listedSpace}:${msg.engine}`;
            const retained = retainCreatedSession(createdSessionFocusRef.current,
              sessionScopeKey(machineId, msg.engine, listedSpace), normalizeSessionList(msg));
            createdSessionFocusRef.current = retained.lease;
            const normalized = retained.sessions;
            const lease = forkFocusLeaseSession(
              forkFocusLeaseRef.current,
              normalized,
              machineId,
              msg.engine,
              listedSpace,
            );
            if (!lease && forkFocusLeaseRef.current
                && forkFocusLeaseRef.current.machineId === machineId
                && forkFocusLeaseRef.current.engine === msg.engine
                && forkFocusLeaseRef.current.space === listedSpace) {
              const materialized = normalized.some(
                (session) => session.session_id
                  === forkFocusLeaseRef.current?.childSessionId,
              );
              clearForkFocusLease(false, !materialized);
            }
            normalizedListedSessions = lease
              ? [lease, ...normalized] : normalized;
            historySessionListsRef.current[
              surfaceKey
            ] = normalizedListedSessions;
            ws.setSessionEngines(normalizedListedSessions);
            sessionListsBySurfaceRef.current[surfaceKey] = normalizedListedSessions;
            authoritativeSurfaceListsRef.current.add(surfaceKey);
            bumpNotificationListRevision();
          }
          if (msg.type === "session_activity") {
            for (const [surfaceKey, listed] of Object.entries(
              sessionListsBySurfaceRef.current,
            )) {
              if (!surfaceKey.endsWith(`:${msg.engine}`)) continue;
              sessionListsBySurfaceRef.current[surfaceKey] = listed.map(
                (session) => session.session_id === msg.session_id
                  ? { ...session, state: msg.state }
                  : session,
              );
            }
          }
          if (msg.type === "state" && msg.sid && ownership) {
            sessionListsBySurfaceRef.current = updateScopedSessionLifecycle(
              sessionListsBySurfaceRef.current,
              ownership.engine,
              ownership.space,
              msg.sid,
              msg.state,
            );
          }
          if (msg.type === "engine_capabilities") {
            acceptSkillCatalog(msg);
          }
          if (msg.type === "turn_end" && msg.sid && msg.notification_context
              && document.hidden
              && typeof Notification !== "undefined"
              && Notification.permission === "granted") {
            const mode = readNotificationMode(localStorage);
            const remoteWillNotify = !msg.to
              && !!pushCoordinatorRef.current?.isRemoteActive(machineId);
            if (mode === "off" || remoteWillNotify) {
              // Web Push owns broadcast completions.
            } else {
              const presentation = turnNotificationPresentation(msg, mode);
              const route = presentation.sessionId
                && presentation.engine && presentation.space
                ? {
                    machine_id: machineId,
                    session_id: presentation.sessionId,
                    engine: presentation.engine,
                    space: presentation.space,
                  }
                : null;
              const url = route ? encodeNotificationRoute(route) : "/";
              void navigator.serviceWorker?.ready.then((registration) =>
                registration.showNotification(presentation.title, {
                  body: presentation.body,
                  icon: "/icon-192.png", badge: "/favicon.svg",
                  tag: turnNotificationTag(msg), data: { url, route },
                })).catch(() => undefined);
            }
          }
          if (msg.type === "session_list") {
            if ((msg.space ?? "code") !== "code" || (msg.engine !== "dsh" && msg.engine !== "codex")) return;
            normalizedListedSessions = mergedAgentSessions(sessionListsBySurfaceRef.current);
          }
          if (msg.type === "session_list") {
            const currentSid = stateRef.current.focusedSid;
            if (!stateRef.current.newChat && !pendingCreateRef.current
                && !createdSessionFocusRef.current
                && currentSid && !currentSid.startsWith("tmp-")
                && !normalizedListedSessions?.some(
                  (session) => session.session_id === currentSid)) {
              didInitFocusRef.current = false;
              preferredSurfaceFocusRef.current = null;
            }
          }
          if (msg.type === "session_list" && normalizedListedSessions) {
            dispatch({ type: "event", event: {
              ...msg, sessions: normalizedListedSessions,
            }, ownership });
          } else {
            dispatch({ type: "event", event: msg, ownership });
          }
          if (msg.type === "wrapper_reconnected") {
            skillCatalogsRef.current = {};
            setSkillCatalogs({});
            skillCatalogRequestsRef.current?.resetReads();
            const focusedSkills = focusedSkillScopeRef.current;
            if (focusedSkills?.engine === "dsh" && focusedSkills.cwd) {
              requestSkillCatalog(focusedSkills, true);
            }
            ws.sendListSessions(engineRef.current, spaceRef.current);
            const currentSid = stateRef.current.focusedSid;
            if (currentSid) requestHistory(
              currentSid, undefined, HISTORY_INITIAL_PAGE, msg.generation);
          }
          // refresh the context ring after each turn (local query, no model tokens)
          if (msg.type === "turn_end" && msg.sid) {
            ws.sendGetContextTo(msg.sid);
            if (sessionActivityPendingRef.current.delete(msg.sid)) {
              const listed = Object.values(sessionListsBySurfaceRef.current)
                .flat().find((session) => session.session_id === msg.sid);
              ws.sendListSessions(
                (listed?.engine as Engine | undefined) ?? engineRef.current,
                listed?.space ?? spaceRef.current,
              );
            }
          }
        },
        onConnState: (s, detail) => {
          if (!acceptsLifecycle()) return;
          dispatch({ type: "conn", connState: s, detail });
          if (s !== "connected") {
            skillCatalogRequestsRef.current?.resetReads();
            // The fork result is authoritative only for this live connection.
            // A reconnect will obtain a fresh native SessionList, so do not
            // preserve a synthetic child indefinitely across disconnects.
            clearForkFocusLease(false);
          }
          if (s === "connected") {
            recoverableReads.clear();
            settleCancelledHistoryBrowse(
              historyRequestsRef.current.beginConnection());
            clearHistoryDetailRequests();
            notificationListRequestRef.current = null;
            bumpNotificationListRevision();
            ws.sendListSessions(engineRef.current, spaceRef.current);
          }
        },
        onResume: () => {
          if (!acceptsLifecycle()) return;
          const engines = new Set<Engine>([engineRef.current]);
          for (const item of stateRef.current.engineCatalog) {
            if (item.available && (item.id === "dsh" || item.id === "codex")) engines.add(item.id);
          }
          for (const kind of engines) ws.sendListSessions(kind, spaceRef.current);
          const sid = stateRef.current.focusedSid;
          if (sid) {
            ws.sendGetStatusTo(sid);
            requestHistory(sid, undefined, HISTORY_INITIAL_PAGE);
          }
        },
        onAuthFail: () => {
          if (!acceptsLifecycle()) return;
          setAuthReady(false);
          clearLegacyAuthMarkers(localStorage);
          pendingCreateRef.current = null;
          createdSessionFocusRef.current = null;
          createRequestsRef.current.clear();
          pendingSessionForkRef.current = null;
          clearForkFocusLease(false);
          historyInvalidationsRef.current.clear();
          historyInvalidationGenerationsRef.current.clear();
          historyCacheEpochRef.current.clear();
          historySessionListsRef.current = {};
          skillCatalogsRef.current = {};
          skillCatalogRequestsRef.current?.reset();
          setSkillCatalogs({});
          setCapabilitiesByScope({});
          inlineImageAssetsRef.current.clear();
          historyImageAssetsRef.current.clear();
          bumpInlineImageRevision();
          bumpHistoryImageRevision();
          historyRequestsRef.current.clear();
          clearHistoryDetailRequests();
          historyPageScopesRef.current.clear();
          void historyPageCacheRef.current.clear();
          composerDraftsRef.current.invalidatePending();
          // Authentication expiry preserves local recovery drafts for re-login.
          setCompletionReceipts({});
          dispatch({ type: "reset" });
          setAuthed(false);
          void (async () => {
            try { await import("./cache").then((module) => module.clearCache()); }
            catch { /* best-effort local cleanup */ }
            setAuthReady(true);
          })();
        },
        onCommandError: (detail) => {
          if (!acceptsLifecycle()) return;
          dispatch({ type: "command_error", detail });
        },
        onOutboxChanged: (protectedSids) => {
          if (!acceptsLifecycle()) return;
          dispatch({ type: "prune_runtimes", protectedSids });
        },
        onWrapperGenerationChanged: () => {
          if (!acceptsLifecycle()) return;
          clearHistoryDetailRequests();
          inlineImageAssetsRef.current.clear();
          historyImageAssetsRef.current.clear();
          bumpInlineImageRevision();
          bumpHistoryImageRevision();
          setCompletionReceipts(discardBtwCompletionReceipts);
        },
      }, machineId);
      effectWs = ws;
      ws.setSurface(engineRef.current, spaceRef.current);
      // Seed both transport and reducer watermarks before Hello. This prevents
      // an older replay/snapshot from reviving a lock already superseded in the
      // last authoritative control snapshot.
      ws.seedReplayState(seeded.cursors, seeded.generations, seeded.controls);
      for (const [sid, control] of Object.entries(seeded.controls)) {
        dispatch({
          type: "hydrate_cache", sid, turns: [], revision: null,
          generation: seeded.generations[sid] ?? control.generation, control,
        });
      }
      wsRef.current = ws;
      ws.start();
    })();

    return () => {
      cancelled = true;
      if (wsLifecycleEpochRef.current === lifecycleEpoch) {
        wsLifecycleEpochRef.current += 1;
      }
      effectWs?.stop();
      if (wsRef.current === effectWs) wsRef.current = null;
      historyRequests.clear();
      clearHistoryDetailRequests();
      recoverableReads.clear();
    };
  }, [
    acceptSkillCatalog,
    authed,
    clearForkFocusLease,
    clearHistoryDetailRequests,
    installBrowseHistoryPage,
    invalidateHistoryPageScopes,
    machineId,
    requestHistory,
    requestSkillCatalog,
    settleCancelledHistoryBrowse,
    startForkFocusLease,
  ]);

  // Land on the preferred/recent session only after an accepted list for the
  // active engine+space arrives. Background snapshots never pick focus.
  useEffect(() => {
    if (pendingNotificationTarget || pendingCreateRef.current) return;
    if (state.newChat && newChatAutoFocus) return;
    if (didInitFocusRef.current || !wsRef.current) return;
    const surfaceKey = `${spaceRef.current}:${engineRef.current}`;
    const focusScopeKey = sessionScopeKey(
      machineId, engineRef.current, spaceRef.current);
    if (!authoritativeSurfaceListsRef.current.has(surfaceKey)) return;
    if (state.sessions.length === 0) {
      preferredSurfaceFocusRef.current = null;
      didInitFocusRef.current = true;
      return;
    }
    const preferred = preferredSurfaceFocusRef.current?.key === focusScopeKey
      ? state.sessions.find((session) => (
          session.session_id === preferredSurfaceFocusRef.current?.sid
          && (session.space ?? "code") === spaceRef.current
          && (session.engine ?? "dsh") === engineRef.current
        ))
      : undefined;
    preferredSurfaceFocusRef.current = null;
    const latest = preferred ?? [...state.sessions]
      .filter((s) => s.tag !== "archived")
      .sort(compareSessionsByActivity)[0]
      ?? state.sessions[0];
    didInitFocusRef.current = true;
    if (latest && latest.session_id !== state.focusedSid) {
      dispatch({ type: "exit_new_chat" });
      dispatch({ type: "focus_session", sid: latest.session_id });
      const latestEngine = latest.engine ?? engineRef.current;
      setEngine(latestEngine);
      wsRef.current.setFocusedSid(latest.session_id, latestEngine, spaceRef.current);
      requestHistory(
        latest.session_id, undefined, HISTORY_INITIAL_PAGE);
      wsRef.current.sendSwitchSession(latest.session_id, latestEngine, spaceRef.current);
    }
  }, [
    machineId,
    pendingNotificationTarget,
    state.sessions,
    state.focusedSid,
    state.newChat,
    newChatAutoFocus,
    requestHistory,
  ]);

  // Direct sidebar selection and newly-created sessions both update the
  // per-surface bookmark.
  useEffect(() => {
    if (!focusedSid || state.newChat) return;
    const selected = state.sessions.find((session) => session.session_id === focusedSid);
    if (!selected) return;
    const selectedEngine = (selected.engine as Engine | undefined) ?? engine;
    const selectedSpace: Space = selected.space === "work" ? "work" : "code";
    lastFocusBySurfaceRef.current[
      sessionScopeKey(machineId, selectedEngine, selectedSpace)
    ] = focusedSid;
  }, [focusedSid, state.newChat, state.sessions, engine, machineId]);

  // Warm the effective Skill catalog when a session becomes usable. Composer
  // completion then reads memory synchronously; an expired entry stays visible
  // while this refresh runs in the background.
  useEffect(() => {
    if (!authed || !focusedSid
        || focusedEngine !== "dsh"
        || state.newChat
        || !capabilityCwd || state.connState !== "connected"
        || !state.wrapperOnline) return;
    requestSkillCatalog({
      key: focusedSkillCatalogKey,
      engine: focusedEngine,
      space,
      cwd: capabilityCwd,
      skillsOnly: true,
      dshSessionId: focusedSid,
    });
  }, [
    authed,
    capabilityCwd,
    focusedEngine,
    focusedSid,
    focusedSkillCatalogKey,
    requestSkillCatalog,
    space,
    state.connState,
    state.newChat,
    state.wrapperOnline,
  ]);

  // An open Extensions sheet follows the focused authorization scope. Switching
  // device, engine, space, or cwd must fetch that scope instead of retaining a
  // report from the previously visible session.
  useEffect(() => {
    if (!capabilitiesOpen || !authed || state.connState !== "connected"
        || !state.wrapperOnline) return;
    setCapabilitiesLoading(true);
    requestSkillCatalog({
      key: focusedSkillCatalogKey,
      engine: focusedEngine,
      space,
      cwd: capabilityCwd,
      skillsOnly: false,
      dshSessionId: focusedEngine === "dsh" ? focusedSid : null,
    }, true);
  }, [
    authed,
    capabilitiesOpen,
    capabilityCwd,
    focusedEngine,
    focusedSid,
    focusedSkillCatalogKey,
    requestSkillCatalog,
    space,
    state.connState,
    state.wrapperOnline,
  ]);

  // Keep a long-lived tab bounded without evicting anything that can still be
  // acted on. ACK callbacks run the same prune when an outbox target becomes
  // reclaimable; otherwise an idle runtime protected during retry would linger.
  useEffect(() => {
    if (Object.keys(state.runtimes).length <= MAX_RUNTIME_SESSIONS) return;
    dispatch({
      type: "prune_runtimes",
      protectedSids: wsRef.current?.pendingSessionIds() ?? [],
    });
  }, [state.runtimes, focusedSid, state.artifact?.sid]);

  // Persist the focused session's turns to IndexedDB (Phase-2 will write through
  // background sessions too). Coalesced in cache.ts.
  useEffect(() => {
    const sid = rt.ccSessionId;
    const revision = rt.historyRevision;
    if (!sid || !revision
        || historyInvalidationsRef.current.has(sid)
        || isHistoryRecoveryPending(state.historyRecovery, sid)) return;
    import("./cache").then(({ saveSession }) => {
      const live = wsRef.current?.lastSeqFor(sid) || 0;
      saveSession(
        sid, rt.turns, live, revision,
        wsRef.current?.generationFor(sid),
        rt.control,
      );
    });
  }, [
    focusedSid, rt.turns, rt.ccSessionId, rt.historyRevision, rt.control,
    state.historyRecovery,
  ]);

  // Race the small authoritative newest page with IndexedDB. A healthy local
  // projection still paints immediately, but a slow/blocked IDB open must never
  // delay the wrapper's four-turn response. hydrate_cache only fills an empty
  // runtime, so whichever source wins cannot clobber the other. A 6s fallback
  // clears the spinner only when both cache and wrapper stay silent.
  useEffect(() => {
    const sid = focusedSid;
    if (!sid) return;
    let cancelled = false;
    let requestFrame: number | null = null;
    const cacheEpoch = historyCacheEpochRef.current.get(sid) ?? 0;
    if (state.connState === "connected") {
      requestFrame = window.requestAnimationFrame(() => {
        requestFrame = null;
        if (!cancelled) {
          requestHistory(sid, undefined, HISTORY_INITIAL_PAGE);
        }
      });
    }
    void import("./cache").then(({ loadSession }) => loadSession(sid)).then((cached) => {
      const valid = !cancelled
          && cacheEpoch === (historyCacheEpochRef.current.get(sid) ?? 0)
          && !historyInvalidationsRef.current.has(sid)
          && cached && Array.isArray(cached.turns)
          && (cached.turns.length || cached.control);
      if (valid && cached) {
        dispatch({
          type: "hydrate_cache", sid,
          turns: (cached.turns as Turn[]).map((turn) => ({
            ...turn, detailLoading: false,
          })),
          revision: cached.revision,
          generation: cached.generation ?? cached.control?.generation,
          control: cached.control,
        });
      }
    });
    const t = window.setTimeout(() => dispatch({
      type: "hydrate_cache", sid, turns: [], revision: null,
    }), 6000);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
      if (requestFrame != null) window.cancelAnimationFrame(requestFrame);
    };
  }, [focusedSid, requestHistory, state.connState]);

  // Cmd/Ctrl+B => toggle sidebar; Cmd/Ctrl+Shift+B => open latest turn's diff.
  useEffect(() => {
    if (!authed) return;
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const k = e.key.toLowerCase();
      if (k === "b" && e.shiftKey) {           // diff (shared right slot)
        e.preventDefault();
        const latest = shortcutRef.current;
        if (latest.artifact?.kind === "gitdiff") dispatch({ type: "clear_artifact" });
        else latest.getDiff("");
      } else if (k === "b") {                    // toggle sidebar
        e.preventDefault();
        setSidebarOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [authed]);

  // Shift+Tab cycles the engine's real permission modes.
  useEffect(() => {
    if (!authed) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Tab" && e.shiftKey) {
        e.preventDefault();
        if ((rt.control && sessionControlLocksInput(rt.control))
            || (!rt.control && rt.external)) return;
        const modes = permsFor(
          focusedEngine, rt.permOptions,
        ).map((p) => p.id);
        if (modes.length === 0) return;
        const current = modes.indexOf(rt.perm);
        setPerm(modes[current < 0 ? 0 : (current + 1) % modes.length]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [authed, focusedSid, rt.perm, rt.permOptions, rt.control, rt.external, focusedEngine]);

  const requestHistoryTurnDetail = useCallback((
    displayTurnId: string, before?: string | null,
    autoLoad = true,
    includeBrowseProjection = true,
  ): boolean => {
    const current = stateRef.current;
    const sid = current.focusedSid;
    const runtime = sid ? current.runtimes[sid] : null;
    if (!sid || !runtime?.historyRevision) return false;
    const browse = includeBrowseProjection && current.historyBrowse?.sid === sid
      ? current.historyBrowse : null;
    const displayed = (browse?.turns ?? runtime.turns).find(
      (turn) => turn.id === displayTurnId
        || canonicalTurnId(turn) === displayTurnId);
    if (!displayed) return false;
    const turnId = canonicalTurnId(displayed);
    const scope = historyPageScopeFor(
      sid, runtime.historyRevision, focusedEngine, space);
    const context: HistoryDetailRequestContext = browse
      ? {
          target: "browse",
          scopeKey: browse.scopeKey,
          sid,
          revision: browse.revision,
          turnId,
          before,
          viewId: browse.viewId,
          windowEpoch: browse.windowEpoch,
        }
      : {
          target: "runtime",
          scopeKey: historyPageCacheScopeKey(scope),
          sid,
          revision: runtime.historyRevision,
          turnId,
          before,
          autoLoad,
        };
    const registration = historyDetailRequestsRef.current.register(context);
    if (!registration.accepted) return false;
    const sent = !registration.send || (
      wsRef.current?.sendGetTurnDetail(
        sid, turnId, context.revision, before) ?? false
    );
    if (!sent) {
      historyDetailRequestsRef.current.cancel(context);
      return false;
    }
    if (context.target === "browse") {
      dispatch({
        type: "history_browse_detail_requested",
        sid,
        scopeKey: context.scopeKey,
        revision: context.revision,
        viewId: context.viewId,
        windowEpoch: context.windowEpoch,
        turnId,
        before,
      });
    } else {
      dispatch({
        type: "turn_detail_requested", sid, turnId, before, autoLoad,
      });
    }
    return true;
  }, [focusedEngine, historyPageScopeFor, space]);
  const loadHistoryTurnDetail = useCallback((
    displayTurnId: string, before?: string | null,
    autoLoad = true,
  ) => requestHistoryTurnDetail(
    displayTurnId, before, autoLoad, true), [requestHistoryTurnDetail]);
  useEffect(() => {
    const current = stateRef.current;
    const sid = current.focusedSid;
    const runtime = sid ? current.runtimes[sid] : null;
    if (!sid || !runtime || current.historyBrowse?.sid === sid) return;
    // Restore only the newest affected row and only its newest detail page.
    // Older rows retain their instant cached projection until the user opens
    // them, avoiding a refresh-triggered multi-megabyte pagination cascade.
    const target = [...runtime.turns].reverse().find((turn) =>
      turn.detailRestorePending === true && turn.detailLoading !== true);
    if (!target) return;
    loadHistoryTurnDetail(target.id, undefined, false);
  }, [loadHistoryTurnDetail, state.historyBrowse, state.runtimes,
    state.focusedSid]);
  useEffect(() => {
    const current = stateRef.current;
    const sid = current.focusedSid;
    const runtime = sid ? current.runtimes[sid] : null;
    if (!sid || !runtime) return;
    const turns = current.historyBrowse?.sid === sid
      ? current.historyBrowse.turns : runtime.turns;
    const next = nextAutoLoadDetailTurn(turns);
    if (!next) return;
    loadHistoryTurnDetail(next.turnId, next.before);
  }, [loadHistoryTurnDetail, state.historyBrowse, state.runtimes,
    state.focusedSid]);

  // Prime the ring as soon as the focused session is usable. Previously the
  // first read only happened when the user opened the popover or a turn ended,
  // so a freshly loaded/restored session misleadingly painted an empty ring.
  useEffect(() => {
    if (!authed || !focusedSid || state.newChat
        || state.connState !== "connected" || !state.wrapperOnline) return;
    if (stateRef.current.runtimes[focusedSid]?.contextRequestId) return;
    const requestId = wsRef.current?.sendGetContext();
    if (requestId) {
      dispatch({ type: "begin_context_request", sid: focusedSid, requestId });
    }
  }, [
    authed,
    focusedEngine,
    focusedSid,
    state.connState,
    state.newChat,
    state.wrapperOnline,
  ]);

  useEffect(() => {
    const artifact = state.artifact;
    const targetSid = artifact?.sid;
    if (!artifact || !targetSid) return;
    const authorization = artifact.authorization?.status === "granted"
      ? artifact.authorization
      : Object.values(artifact.assets ?? {}).find(
        (asset) => asset.authorization?.status === "granted",
      )?.authorization;
    if (!authorization) return;
    const ws = wsRef.current;
    const sent = authorization.operation === "file_preview"
      ? !!ws?.sendGetFilePreview(
        authorization.path,
        authorization.requestId,
        targetSid,
      )
      : !!authorization.previewId && !!ws?.sendGetPreviewAsset(
        authorization.path,
        authorization.previewId,
        authorization.requestId,
        targetSid,
      );
    dispatch({
      type: sent
        ? "preview_authorization_retry_started"
        : "preview_authorization_retry_failed",
      sid: targetSid,
      authorizationId: authorization.authorizationId,
      requestId: authorization.requestId,
    });
  }, [state.artifact]);

  if (!authReady) {
    return <div className="login" aria-busy="true">正在连接中继…</div>;
  }

  if (!authed) {
    return <LoginForm onLogin={() => { dispatch({ type: "reset" }); setAuthed(true); }} theme={theme} onToggleTheme={toggleTheme} />;
  }

  const sendDeferredQuery = (
    sid: string,
    query: PendingQuery,
    delivery: "queue" | "replace",
  ): boolean => {
    const ws = wsRef.current;
    if (!ws) return false;
    const currentState = stateRef.current;
    const unconfirmed = collectUnconfirmedQueries(
      currentState.runtimes,
      delivery === "replace" ? sid : undefined,
    );
    if (!canEnqueueQuery(
      unconfirmed,
      query,
      deferredQueueCapacity(
        currentState, delivery === "replace" ? sid : undefined),
    )) {
      dispatch({
        type: "command_error",
        detail: "排队已满（最多 32 条 / 64 MiB），请先等待发送。",
      });
      return false;
    }
    const msg_id = uuid();
    if (!ws.sendDeferredQueryTo(
      sid, query.prompt, msg_id, delivery, query.images, query.files,
    )) return false;
    const surfaceKey = `${space}:${engine}`;
    const cached = sessionListsBySurfaceRef.current[surfaceKey];
    if (cached) sessionListsBySurfaceRef.current[surfaceKey] = bumpSessionActivity(cached, sid, Date.now());
    sessionActivityPendingRef.current.add(sid);
    const currentReplacement = currentState.runtimes[sid]?.pendingSend;
    const optimistic: PendingQuery = {
      ...query,
      msg_id,
      imageCount: query.images?.length,
      fileCount: query.files?.length,
      queueKind: delivery,
      queueState: "submitting",
      retainedBytes: undefined,
      queueError: undefined,
      failedAt: undefined,
      replacesRetainedBytes: delivery === "replace"
        ? currentReplacement?.queueState === "queued"
          ? currentReplacement.retainedBytes
          : currentReplacement?.replacesRetainedBytes
        : undefined,
    };
    dispatch(delivery === "queue"
      ? { type: "enqueue", sid, query: optimistic }
      : { type: "set_pending", sid, query: optimistic });
    return true;
  };

  const cancelQueuedQuery = (sid: string, query: PendingQuery): void => {
    if (!query.msg_id) return;
    if (query.queueState !== "failed"
        && !wsRef.current?.sendCancelQueuedQueryTo(sid, query.msg_id)) {
      return;
    }
    dispatch({ type: "remove_deferred", sid, msgId: query.msg_id });
  };

  const inspectQueuedQuery = (sid: string, query: PendingQuery, actions = false, takeback = false): void => {
    if (!query.msg_id) return;
    const serverOwned = query.queueState === "queued";
    const requestId = serverOwned
      ? wsRef.current?.sendGetQueuedQueryTo(sid, query.msg_id,
          !actions && (queuedEditHoldSupported || takeback) ? "begin" : undefined) ?? null
      : null;
    setQueuedQueryEditor({
      sid,
      msgId: query.msg_id,
      actions,
      editingHeld: serverOwned && !actions && (queuedEditHoldSupported || takeback),
      takeback,
      preview: query.prompt,
      prompt: serverOwned ? null : query.prompt,
      kind: query.queueKind ?? null,
      state: query.queueState ?? "submitting",
      imageCount: query.imageCount ?? query.images?.length ?? 0,
      fileCount: query.fileCount ?? query.files?.length ?? 0,
      loading: serverOwned && requestId !== null,
      saving: false,
      error: serverOwned && requestId === null
        ? "完整消息读取请求未发送，请等待连接恢复后重试。"
        : query.queueError ?? null,
      detailRequestId: requestId,
      updateRequestId: null,
      pendingPrompt: null,
    });
  };

  const updateQueuedQuery = (prompt: string, startNow = false, addImages?: QueryImg[], addFiles?: QueryFile[]): boolean => {
    const current = queuedQueryEditor;
    if (!current || current.saving) return false;
    if (startNow && (current.state !== "queued"
        || !stateRef.current.runtimes[current.sid]?.queuedStartNowSupported)) return false;
    if (current.state === "failed") {
      dispatch({
        type: "update_failed_deferred",
        sid: current.sid,
        msgId: current.msgId,
        prompt, addImages, addFiles,
      });
      setQueuedQueryEditor(null);
      return true;
    }
    if (current.state !== "queued") return false;
    const requestId = wsRef.current?.sendUpdateQueuedQueryTo(
      current.sid, current.msgId, prompt, startNow, addImages, addFiles) ?? null;
    if (requestId === null) {
      setQueuedQueryEditor((editor) => editor ? {
        ...editor,
        error: "修改请求未发送，请等待连接恢复后重试。",
      } : null);
      return false;
    }
    setQueuedQueryEditor((editor) => (
      editor
      && editor.sid === current.sid
      && editor.msgId === current.msgId
        ? {
            ...editor,
            saving: true,
            error: null,
            updateRequestId: requestId,
            pendingPrompt: prompt,
            pendingStartNow: startNow,
          }
        : editor
    ));
    return true;
  };

  const retryQueuedQuery = (): boolean => {
    const current = queuedQueryEditor;
    if (!current || current.saving || current.prompt === null) return false;
    if (current.state === "queued") {
      return updateQueuedQuery(current.prompt);
    }
    if (current.state !== "failed") return false;
    const runtime = stateRef.current.runtimes[current.sid];
    const query = runtime?.failedDeferred.find(
      (candidate) => candidate.msg_id === current.msgId);
    if (!query) return false;
    const sent = sendDeferredQuery(
      current.sid,
      { ...query, prompt: current.prompt },
      query.queueKind === "replace" ? "replace" : "queue",
    );
    if (!sent) return false;
    dispatch({
      type: "remove_deferred",
      sid: current.sid,
      msgId: current.msgId,
    });
    setQueuedQueryEditor(null);
    return true;
  };

  const sendQuery = (prompt: string, images?: QueryImg[], files?: QueryFile[]): boolean => {
    const ws = wsRef.current;
    if (!ws || !focusedSid) return false;
    // Every ordinary send transfers ownership to the wrapper FIFO. Browser
    // runtime/list states may disagree during reconnects and edit transitions.
    return sendDeferredQuery(focusedSid,{prompt,images,files},"queue");
  };

  const loadOlderHistoryPage = (
    anchorTurnId?: string,
  ): boolean | {
    accepted: true;
    viewId: string;
    scopeKey: string;
    generation: string | null;
  } => {
    const current = stateRef.current;
    const sid = current.focusedSid;
    const runtime = sid ? current.runtimes[sid] : null;
    if (!sid || !runtime?.historyRevision || !wsRef.current
        || runtime.historyInvalidated
        || isHistoryRecoveryPending(current.historyRecovery, sid)) return false;
    const existing = current.historyBrowse?.sid === sid
      ? current.historyBrowse : null;
    if (existing) {
      if (!existing.hasOlder || !existing.olderCursor
          || !historyPageScopesRef.current.has(existing.scopeKey)) return false;
      const context: HistoryBrowseRequestContext = {
        scopeKey: existing.scopeKey,
        viewId: existing.viewId,
        windowEpoch: existing.windowEpoch,
        pendingBefore: existing.olderCursor,
        sourcePageKey: existing.oldestPageKey,
        anchorTurnId: anchorTurnId ?? null,
      };
      return requestHistory(
        sid, existing.olderCursor, HISTORY_MORE_PAGE,
        existing.generation, existing.revision, context);
    }
    if (!runtime.hasMore || !runtime.oldestId) return false;
    const scope = historyPageScopeFor(
      sid, runtime.historyRevision, focusedEngine, space);
    const scopeKey = historyPageCacheScopeKey(scope);
    const viewId = uuid();
    const basePageKey = `${HISTORY_LATEST_PAGE_KEY}:${viewId}`;
    const browseGeneration = runtime.historyGeneration
      ?? wsRef.current.generationFor(sid)
      ?? null;
    const basePage: HistoryBrowsePage = {
      pageKey: basePageKey,
      turns: runtime.turns,
      hasOlder: true,
      olderCursor: runtime.oldestId,
      hasNewer: false,
      newerPageKey: null,
      isLatest: true,
    };
    void historyPageCacheRef.current.putPage(scope, basePage);
    dispatch({
      type: "begin_history_browse",
      sid,
      scopeKey,
      revision: runtime.historyRevision,
      generation: browseGeneration,
      viewId,
      basePageKey,
    });
    const accepted = requestHistory(
      sid, runtime.oldestId, HISTORY_MORE_PAGE,
      browseGeneration, runtime.historyRevision, {
        scopeKey,
        viewId,
        windowEpoch: 0,
        pendingBefore: runtime.oldestId,
        sourcePageKey: basePageKey,
        anchorTurnId: anchorTurnId ?? null,
      });
    return accepted ? {
      accepted: true,
      viewId,
      scopeKey,
      generation: browseGeneration,
    } : false;
  };
  const loadNewerHistoryPage = (anchorTurnId?: string): boolean => {
    const browse = stateRef.current.historyBrowse;
    if (!browse || !browse.hasNewer || !browse.newerPageKey) return false;
    const scope = historyPageScopesRef.current.get(browse.scopeKey);
    if (!scope) return false;
    const frozen = {
      sid: browse.sid,
      scopeKey: browse.scopeKey,
      revision: browse.revision,
      generation: browse.generation,
      viewId: browse.viewId,
      windowEpoch: browse.windowEpoch,
      pageKey: browse.newerPageKey,
      anchorTurnId: anchorTurnId ?? null,
    };
    void (async () => {
      const page = await historyPageCacheRef.current.getPage(
        scope, frozen.pageKey);
      const currentState = stateRef.current;
      const current = currentState.historyBrowse;
      if (currentState.focusedSid !== frozen.sid
          || !current
          || !acceptsCachedNewerPage(current, frozen)) return;
      if (page?.isLatest && current.latestDirty) {
        dispatch({
          type: "history_browse_newer_settled",
          sid: frozen.sid,
          scopeKey: frozen.scopeKey,
          revision: frozen.revision,
          generation: frozen.generation,
          viewId: frozen.viewId,
          windowEpoch: frozen.windowEpoch,
          pageKey: frozen.pageKey,
        });
        return;
      }
      if (!page) {
        dispatch({
          type: "history_browse_newer_unavailable",
          sid: frozen.sid,
          scopeKey: frozen.scopeKey,
          revision: frozen.revision,
          generation: frozen.generation,
          viewId: frozen.viewId,
          windowEpoch: frozen.windowEpoch,
        });
        return;
      }
      const protectedTurnIds = protectedHistoryTurnIds(
        frozen.anchorTurnId,
        textSelectionGuardRef.current,
        {
          sid: frozen.sid,
          revision: frozen.revision,
          viewId: frozen.viewId,
          scopeKey: frozen.scopeKey,
        },
      );
      const mutation = appendNewerPage(current, page, {
        expectedScopeKey: frozen.scopeKey,
        expectedViewId: frozen.viewId,
        expectedWindowEpoch: frozen.windowEpoch,
        expectedNewerPageKey: frozen.pageKey,
        protectedTurnIds,
      });
      if (mutation.projection === current) return;
      for (const evicted of mutation.evictedPages) {
        void historyPageCacheRef.current.putPage(scope, evicted);
      }
      dispatch({
        type: "install_history_browse_newer",
        sid: frozen.sid,
        scopeKey: frozen.scopeKey,
        revision: frozen.revision,
        generation: frozen.generation,
        viewId: frozen.viewId,
        windowEpoch: frozen.windowEpoch,
        page,
        protectedTurnIds,
        prepared: {
          from: current,
          to: mutation.projection,
        },
      });
    })();
    return true;
  };
  const returnToLatestHistory = () => {
    const sid = stateRef.current.historyBrowse?.sid;
    if (sid) dispatch({ type: "return_to_latest", sid });
  };
  // One command creates the session and starts its first query atomically. The
  // wrapper targets the new temp-keyed ctx directly; no later focus event is used
  // to route or trigger this message.
  const startNewChat = (explicitCwd?: string) => {
    if (!confirmArtifactDiscard()) return;
    clearForkFocusLease(false);
    cancelPendingNotificationTarget();
    pendingCreateRef.current = null;
    createdSessionFocusRef.current = null;
    didInitFocusRef.current = true;
    setCreateError(null);
    setNewChatAutoFocus(true);
    wsRef.current?.setFocusedSid(null);
    setEngine(focusedEngine);
    const cwd = explicitCwd || currentCwd || "~";
    dispatch({ type: "enter_new_chat", cwd,
      cwdSource: explicitCwd ? "explicit" : currentCwd ? "inherited" : "default",
      dshAgentPreset: state.defaultDshPresetId });
    if (isMobile()) setSidebarOpen(false);
  };

  const sendFirstMessage = (prompt: string, images?: QueryImg[], files?: QueryFile[]): boolean => {
    if (!wsRef.current || !state.newChat
        || (engine === "dsh" && (space !== "code" || !!files?.length))) {
      return false;
    }
    const { cwd, cwdSource, model, effort } = state.newChat;
    // Null is meaningful: let the local engine use its configured defaults.
    // Only explicit user choices cross the wire; otherwise a stale fallback
    // catalog could silently override the machine's real model configuration.
    const msg_id = uuid();
    const queued = wsRef.current.sendNewSession(
      cwd, engine, model, effort,
      { prompt, msg_id, images, files },
      space,
      engine === "dsh" ? state.newChat.dshAgentPreset : undefined);
    if (queued) {
      pendingCreateRef.current = msg_id;
      createRequestsRef.current.set(msg_id, {
        scopeKey: sessionScopeKey(machineId, engine, space),
        cwdSource,
        draftKey:composerDraftKey(machineId,space,engine,`new:${state.newChat.cwd}`),
      });
      while (createRequestsRef.current.size > 64) {
        const oldest = createRequestsRef.current.keys().next().value;
        if (!oldest) break;
        createRequestsRef.current.delete(oldest);
      }
      setCreateError(null);
    }
    return queued;
  };
  const pickNewChatModel = (model: string | null) => {
    const current = state.newChat;
    if (!current) return;
    const compatibleEffort = compatibleNewChatEffort(
      engine,
      model,
      current.effort,
      newChatCatalog,
      newChatDefaults.model,
    );
    dispatch({ type: "set_new_chat_model", model });
    if (compatibleEffort !== current.effort) {
      dispatch({
        type: "set_new_chat_effort",
        effort: compatibleEffort,
      });
    }
  };
  const pickNewChatEffort = (effort: string | null) => {
    if (!state.newChat) return;
    dispatch({ type: "set_new_chat_effort", effort });
  };
  const pickNewChatDshPreset = (presetId: string | null) => {
    if (engine !== "dsh") return;
    dispatch({ type: "set_new_chat_dsh_preset", presetId });
  };
  const interrupt = () => wsRef.current?.sendInterrupt();
  const setModel = (model: string) => {
    wsRef.current?.sendSetModel(model);
  };
  const setEffort = (effort: string) => {
    wsRef.current?.sendSetEffort(effort);
  };
  const setPerm = (perm: string) => {
    if (perm === stateRef.current.runtimes[stateRef.current.focusedSid ?? ""]?.perm) return;
    if (perm === "danger-full-access"
        && !window.confirm("启用 Full access？")) return;
    wsRef.current?.sendSetPerm(perm);
  };
  const requestContext = () => {
    if (!focusedSid) return;
    const requestId = wsRef.current?.sendGetContext();
    if (requestId) {
      dispatch({ type: "begin_context_request", sid: focusedSid, requestId });
    }
  };
  const forkFromTurn = (forkPointId: string) => {
    if (!focusedSid || pendingSessionForkRef.current) return;
    const requestId = wsRef.current?.sendForkSession(
      focusedSid, forkPointId) ?? null;
    if (!requestId) {
      dispatch({ type: "command_error",
        detail: "派生请求未发送，请等待连接恢复后重试。" });
      return;
    }
    pendingSessionForkRef.current = {
      requestId,
      parentSessionId: focusedSid,
      forkPointId,
      engine: focusedEngine,
    };
  };
  const getDiff = (file: string) => {
    if (!confirmArtifactDiscard()) return;
    const requestId = wsRef.current?.sendGetDiff(file, theme) ?? null;
    if (!requestId) return;
    dispatch({ type: "open_artifact_loading", file, sid: focusedSid, requestId });
  };
  const openTurnDiff = (files: string[], diff: string) => {
    if (!diff || !confirmArtifactDiscard()) return;
    dispatch({ type: "set_artifact", artifact: {
      file: files.length === 1 ? files[0] : `本轮改动 · ${files.length} 个文件`,
      sid: focusedSid,
      kind: "gitdiff",
      sections: parseGitDiff(diff),
    } });
  };
  const previewFileForSid = (
    targetSid: string | null,
    file: string,
    line?: number,
  ) => {
    if (!targetSid) return;
    if (!confirmArtifactDiscard()) return;
    const requestId = uuid();
    if (!wsRef.current?.sendGetFilePreview(file, requestId, targetSid)) return;
    dispatch({
      type: "open_file_loading",
      file,
      sid: targetSid,
      requestId,
      kind: isMarkdownPath(file) ? "md" : "file",
      line,
    });
  };
  const previewFile = (file: string, line?: number) =>
    previewFileForSid(focusedSid, file, line);
  const previewArtifactFile = (file: string, line?: number) =>
    previewFileForSid(state.artifact?.sid ?? focusedSid, file, line);
  const previewMarkdown = (file: string) => previewFile(file);
  const loadPreviewAsset = (file: string, previewId: string): boolean => {
    const targetSid = state.artifact?.sid ?? focusedSid;
    if (!targetSid) return false;
    const requestId = uuid();
    if (!wsRef.current?.sendGetPreviewAsset(
      file, previewId, requestId, targetSid)) return false;
    dispatch({
      type: "begin_preview_asset",
      sid: targetSid,
      path: file,
      previewId,
      requestId,
    });
    return true;
  };
  const authorizePreview = (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ): boolean => {
    const artifact = stateRef.current.artifact;
    const targetSid = artifact?.sid;
    if (!artifact || !targetSid) return false;
    const activeAuthorization = artifact.authorization
      ?? Object.values(artifact.assets ?? {}).find(
        (asset) => (
          asset.authorization?.authorizationId
            === authorization.authorizationId
          && asset.authorization.requestId === authorization.requestId
        ),
      )?.authorization;
    if (!activeAuthorization
        || activeAuthorization.authorizationId
          !== authorization.authorizationId
        || activeAuthorization.requestId !== authorization.requestId
        || activeAuthorization.status !== "required") return false;
    if (!wsRef.current?.sendAuthorizePreview(
      authorization.authorizationId,
      authorization.requestId,
      decision,
      targetSid,
    )) return false;
    dispatch({
      type: "submit_preview_authorization",
      sid: targetSid,
      authorizationId: authorization.authorizationId,
      requestId: authorization.requestId,
    });
    return true;
  };
  const saveMarkdown = (file: string, content: string, expectedSize: number,
                        expectedMtimeNs: string, expectedRevision: string): string | null => {
    const targetSid = state.artifact?.sid ?? focusedSid;
    if (!targetSid) return null;
    const requestId = uuid();
    if (!wsRef.current?.sendSaveMarkdown(
      file, content, expectedSize, expectedMtimeNs, expectedRevision,
      requestId, targetSid)) return null;
    dispatch({ type: "start_file_save", requestId, content });
    return requestId;
  };
  shortcutRef.current = {
    artifact: state.artifact,
    getDiff,
  };
  const logout = async () => {
    composerDraftsRef.current.invalidatePending();
    try {
      const response = await fetch("/api/logout", {
        method: "POST", credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await import("./cache").then((module) => module.clearCache());
      await historyPageCacheRef.current.clear();
      wsRef.current?.stop();
      pendingCreateRef.current = null;
      createdSessionFocusRef.current = null;
      createRequestsRef.current.clear();
      pendingSessionForkRef.current = null;
      clearForkFocusLease(false);
      sessionActivityPendingRef.current.clear();
      historyInvalidationsRef.current.clear();
      historyInvalidationGenerationsRef.current.clear();
      historyCacheEpochRef.current.clear();
      historyRequestsRef.current.clear();
      clearHistoryDetailRequests();
      historyPageScopesRef.current.clear();
      composerDraftsRef.current.clear();
      await composerDraftsRef.current.flush();
      setCreateError(null);
      setCompletionReceipts({});
      dispatch({ type: "reset" });
      setAuthed(false);
    } catch {
      dispatch({ type: "command_error", detail: "退出失败：服务暂不可用，请稍后重试" });
    }
  };
  const updateNotificationMode = async (mode: NotificationMode): Promise<boolean> => {
    if (mode !== "off") {
      if (typeof Notification === "undefined") {
        dispatch({ type: "command_error", detail: "当前浏览器不支持系统通知。" });
        return false;
      }
      const permission = Notification.permission === "granted"
        ? "granted" : await Notification.requestPermission();
      if (permission !== "granted") {
        dispatch({ type: "command_error", detail: "通知权限未开启，设置没有更改。" });
        return false;
      }
    }
    writeNotificationMode(localStorage, mode);
    setNotificationMode(mode);
    return true;
  };
  const activeDevice = remoteDevices.find(
    (device) => device.machine_id === machineId);
  const activeDeviceOnline = state.connState === "connected" && state.wrapperOnline;
  // A native client can advance the transcript without a wrapper-owned turn.
  // Present that mirrored activity as running in every status surface while
  // leaving Composer on the authoritative write state (so a read-only App turn
  // never gains a Stop button it cannot actually control).
  const focusedSessionState = state.sessions.find(
    (session) => session.session_id === focusedSid)?.state;
  const effectiveState = mergeSessionActivityState(
    focusedSessionState, rt.state, rt.mirroredRunning,
  ) ?? rt.state;

  if (!draftsReady) return <div className="draft-loading" role="status">正在恢复本地草稿…</div>;

  return (
    <div className={"shell" + (sidebarOpen ? " sidebar-open" : "") + (state.artifact ? " panel-open" : "")} ref={drawer.shellRef}
      onTouchStart={drawer.onTouchStart} onTouchMove={drawer.onTouchMove} onTouchEnd={drawer.onTouchEnd}
      onPointerDownCapture={drawer.resetClick}
      onClickCapture={event=>{if(drawer.suppressClick.current && event.detail>0){event.preventDefault();event.stopPropagation();}}}
      onTouchCancel={drawer.onTouchCancel}>
      {accountUsageOpen && <Suspense fallback={null}><AccountUsageSheet
        key={machineId} engines={state.engineCatalog} reports={accountUsageReports}
        serviceReady={accountUsageSupported}
        onRequest={agent => {
          if (!accountUsageSupported) return null;
          const id = wsRef.current?.sendGetAccountUsage(agent) ?? null;
          if (id) accountUsageRequests.current[agent] = id;
          return id;
        }}
        onClose={() => setAccountUsageOpen(false)} /></Suspense>}
      <SessionsSidebar
        machineId={machineId}
        side={drawer.side}
        open={sidebarOpen}
        engine={engine}
        space={space}
        onSpaceChange={switchSpace}
        sessions={state.sessions}
        liveStates={Object.fromEntries(state.sessions.map((session) => {
          const runtime = state.runtimes[session.session_id];
          return [session.session_id, mergeSessionActivityState(
            session.state,
            runtime?.state,
            runtime?.mirroredRunning,
          ) ?? "idle"];
        }))}
        completionBadges={completionBadges}
        activeSessionId={focusedSid}
        onSelect={(id) => {
          if (!confirmArtifactDiscard()) return;
          cancelPendingNotificationTarget();
          const selected = state.sessions.find((s) => s.session_id === id);
          if (selected) focusListedSession(selected);
        }}
        onNew={() => startNewChat()}
        onNewInDir={(cwd) => startNewChat(cwd)}
        onClose={() => setSidebarOpen(false)}
        onRename={(id, title) => {
          const session = stateRef.current.sessions.find(item => item.session_id === id);
          const target = session ? sessionCommandTarget(session, engine, space) : {engine, space};
          wsRef.current?.sendRenameSession(id, title, target.engine, target.space);
        }}
        onArchive={(id, archived) => { wsRef.current?.sendArchiveSession(id, archived, engine, space); }}
        onPin={(session, pinned) => {
          const target = sessionCommandTarget(session, engine, space);
          const surfaceKey = `${target.space}:${target.engine}`;
          const cached = sessionListsBySurfaceRef.current[surfaceKey];
          if (cached) {
            sessionListsBySurfaceRef.current[surfaceKey] = setSessionPinned(
              cached, session.session_id, pinned);
          }
          dispatch({ type: "set_session_pinned", sid: session.session_id, pinned });
          wsRef.current?.sendPinSession(
            session.session_id, pinned, target.engine, target.space);
        }}
        onDelete={(id) => {
          const warning = "删除后将永久移除这条会话历史；代码文件不会被删除，确定继续吗？";
          if (!window.confirm(warning)) return;
          if (createdSessionFocusRef.current?.session.session_id === id) createdSessionFocusRef.current = null;
          const deleted = state.sessions.find(
            (session) => session.session_id === id);
          const target = deleted
            ? sessionCommandTarget(deleted, engine, space)
            : { engine, space };
          if (forkFocusLeaseRef.current?.childSessionId === id) {
            clearForkFocusLease(false);
          }
          composerDraftsRef.current.delete(composerDraftKey(
            machineId, target.space, target.engine, id,
          ));
          invalidateHistoryPageScopes(id);
          if (focusedSid === id) clearHistoryDetailRequests();
          if (focusedSid === id) dispatch({
            type: "enter_new_chat", cwd: "~", cwdSource: "default",
            dshAgentPreset: state.defaultDshPresetId,
          });
          wsRef.current?.sendDeleteSession(
            id, target.engine, target.space);
        }}
      />
      {fileBrowserOpen && focusedSid && <FileBrowser key={`${machineId}:${focusedSid}`}
        cwd={currentCwd || "当前项目"} sid={focusedSid} response={filesList}
        onBrowse={(path, query) => wsRef.current?.sendBrowseFiles(path, query, focusedSid) ?? null}
        onOpenFile={(path) => { previewFile(path); setFileBrowserOpen(false); }}
        onClose={() => setFileBrowserOpen(false)} />}
      <DirPicker
        open={dirPickerOpen}
        path={state.dirPicker?.path ?? null}
        parent={state.dirPicker?.parent ?? null}
        dirs={state.dirPicker?.dirs ?? []}
        responseRequestId={state.dirPicker?.requestId ?? null}
        onBrowse={(p) => wsRef.current?.sendListDir(p) ?? null}
        onConfirm={(cwd) => { if (state.newChat) dispatch({ type: "set_new_chat_cwd", cwd, cwdSource: "explicit" }); setDirPickerOpen(false); }}
        onClose={() => setDirPickerOpen(false)}
      />
      <section className={`pane code-pane`}>
        <header className={`c-head code-head`}>
          <button type="button" className="titlewrap session-heading-trigger"
            aria-label="打开会话侧边栏" aria-expanded={sidebarOpen}
            onClick={() => setSidebarOpen(value => !value)}>
            <div className="ttl">
              <div className="surface-head-title">
                <span>{state.newChat ? "新会话" : focusedSession?.summary || focusedSession?.first_prompt || "未命名会话"}</span>
              </div>
              {focusedDshPresetLabel && (
                <span className="dsh-agent-preset"
                  title={`DSH Agent：${focusedDshPresetLabel}`}>
                  Agent · {focusedDshPresetLabel}
                </span>
              )}
            </div>
            <div className="sub session-context">
              <AgentBadge engine={state.newChat ? engine : focusedEngine} className="session-agent-badge" />
              <span className="session-heading-directory" title={currentCwd || ""}>{currentCwd?.replace(/\/+$/, "").split("/").at(-1) || "未选择目录"}</span>
              {focusedSession?.git_branch && <span className="session-heading-branch" title={`Git 分支 ${focusedSession.git_branch}`}><Icon name="branch" size={13} />{focusedSession.git_branch}</span>}
            </div>
          </button>
          <div className="header-utilities">
          {!state.newChat && <span className={`hstat ${effectiveState}`}><span className="sd" />
            <span className="hstat-label">{effectiveState}</span></span>}
          <button className={`device-trigger${activeDeviceOnline ? " online" : ""}`}
            onClick={() => setDeviceSheetOpen(true)} aria-label="设备中心"
            title={`${activeDevice?.label ?? machineId} · ${activeDeviceOnline ? "在线" : "离线"}`}>
            <Icon name="devices" size={18} />
            <i aria-hidden="true" />
          </button>
          <HeaderMenu
            theme={theme}
            appearance={appearance}
            onAppearance={setAppearance}
            chatSpacing={chatSpacing} onChatSpacing={setChatSpacing}
            onAccountUsage={() => setAccountUsageOpen(true)}
            notificationMode={notificationMode}
            notificationBinding={pushBinding.state}
            notificationAvailable={typeof Notification !== "undefined"}
            onNotificationMode={updateNotificationMode}
            onToggleTheme={toggleTheme}
            onBrowseFiles={focusedSid && !state.newChat ? () => setFileBrowserOpen(true) : undefined}
            onLogout={() => void logout()}
          />
          </div>
        </header>

        <ReconnectBanner banner={state.banner}
          sticky={state.connState !== "connected"}
          onReconnect={() => wsRef.current?.reconnectNow("正在重新连接…")}
          replaying={rt.replaying || historyView.recovering}
          truncated={rt.truncated}
          busy={state.connState !== "connected" || !state.wrapperOnline
            || rt.replaying || historyView.recovering}
          onDismiss={dismissBanner} />
        <NoticeStack notices={rt.notices}
          onDismiss={(noticeId) => {
            if (focusedSid) dispatch({ type: "dismiss_notice", sid: focusedSid, noticeId });
          }} />

        {state.newChat ? (
          <NewChatView cwd={state.newChat.cwd}
            appearance={appearance}
            draftStore={composerDraftsRef.current}
            draftKey={composerDraftKey(machineId, space, engine, `new:${state.newChat.cwd}`)}
            createError={createError}
            autoFocus={newChatAutoFocus}
            engine={engine}
            engines={state.engineCatalog}
            onPickEngine={next => {
              if (next === engine || !state.engineCatalog.some(item => item.id === next && item.available)) return;
              const sourceKey = composerDraftKey(machineId, space, engine, `new:${state.newChat!.cwd}`);
              const destinationKey = composerDraftKey(machineId, space, next, `new:${state.newChat!.cwd}`);
              const draft = composerDraftsRef.current.get(sourceKey);
              if (draftHasContent(draft)) {
                const previous = composerDraftsRef.current.get(destinationKey);
                if (draftHasContent(previous)) composerDraftsRef.current.archive(destinationKey, previous, "replaced");
                composerDraftsRef.current.set(destinationKey, draft);
              }
              setEngine(next);
              dispatch({type:"set_new_chat_selection",model:null,effort:null});
            }}
            catalog={newChatCatalog}
            model={state.newChat.model}
            effort={state.newChat.effort}
            defaultModel={newChatDefaults.model}
            defaultEffort={newChatDefaults.effort}
            dshPresets={state.dshPresets}
            defaultDshPresetId={state.defaultDshPresetId}
            dshAgentPreset={state.newChat.dshAgentPreset}
            onOpenSessions={() => setSidebarOpen(true)}
            onPickCwd={() => setDirPickerOpen(true)}
            onPickModel={pickNewChatModel}
            onPickEffort={pickNewChatEffort}
            onPickDshPreset={pickNewChatDshPreset}
            onSend={sendFirstMessage} />
        ) : (
          <>
            <ChatView key={`${activeScopeKey}\u0000${focusedSid ?? ""}`}
              sid={focusedSid} turns={historyView.turns}
              submittedMessageId={rt.lastSubmittedMsgId}
              loading={!!rt.loading || historyView.recovering}
              engine={focusedEngine}
              hasMore={historyView.hasMore}
              historyPagingReady={historyView.pagingReady}
              historyRevision={rt.historyRevision}
              historyViewRevision={historyView.viewRevision}
              historyGeneration={historyView.generation}
              historyViewId={historyView.viewId}
              historyScopeKey={historyView.scopeKey ?? activeScopeKey}
              historyWindowEpoch={historyView.windowEpoch}
              historyCursor={historyView.oldestId}
              browseMode={historyView.browsing}
              hasNewer={historyView.hasNewer}
              onLoadMore={loadOlderHistoryPage}
              onLoadNewer={loadNewerHistoryPage}
              onReturnLatest={returnToLatestHistory}
              onLoadDetail={historyView.recovering
                ? undefined : loadHistoryTurnDetail}
              onEdit={historyView.recovering
                ? undefined : (prompt) => setEditPrompt(prompt)}
              onGetDiff={historyView.recovering ? undefined : getDiff}
              onOpenTurnDiff={historyView.recovering
                ? undefined : openTurnDiff}
              onPreviewMarkdown={historyView.recovering
                ? undefined : previewMarkdown}
              onOpenFile={historyView.recovering ? undefined : previewFile}
              imageAssets={inlineImageAssets}
              onLoadImage={historyView.recovering
                ? undefined : loadFocusedMessageImage}
              onAuthorizeImage={historyView.recovering
                ? undefined : authorizeMessageImage}
              historyImageAssets={historyImageAssets}
              onLoadHistoryImage={historyView.recovering
                ? undefined : loadHistoryImage}
              onTextSelectionGuardChange={updateTextSelectionGuard}
              onFork={!historyView.recovering
                ? forkFromTurn : undefined} />

            <Composer
              appearance={appearance}
              draftLabel={focusedSession?.summary || currentCwd || "会话草稿"}
              draftKey={focusedComposerDraftKey}
              draftStore={composerDraftsRef.current}
              state={effectiveState}
              catalog={focusedCatalog}
              connState={state.connState}
              wrapperOnline={state.wrapperOnline}
              sendMode={rt.sendMode}
              setSendMode={(m) => focusedSid && dispatch({
                type: "set_send_mode", sid: focusedSid, mode: m,
              })}
              queue={rt.queue}
              pendingSend={rt.pendingSend}
              failedDeferred={rt.failedDeferred}
              unconfirmedQueued={unconfirmedQueued}
              unconfirmedReplaceable={unconfirmedReplaceable}
              queueCapacity={queueCapacity}
              replaceQueueCapacity={replaceQueueCapacity}
              model={rt.model}
              effort={rt.effort}
              perm={rt.perm}
              permOptions={rt.permOptions}
              control={rt.control}
              external={rt.external}
              takeoverPending={rt.takeoverPending}
              takeoverMessage={rt.takeoverMessage}
              engine={focusedEngine}
              editPrompt={editPrompt}
              onEditConsumed={() => setEditPrompt(null)}
              restoreQuery={queuedTakeback}
              onRestoreQueryConsumed={() => setQueuedTakeback(null)}
              onSendQuery={sendQuery}
              onInterrupt={interrupt}
              onOpenSessions={() => setSidebarOpen(true)}
              questionOpen={!!rt.pendingQuestion}
              navigationOpen={sidebarOpen}
              onEnqueue={(query) => (
                focusedSid ? sendDeferredQuery(focusedSid, query, "queue") : false
              )}
              onSetPending={(query) => (
                focusedSid ? sendDeferredQuery(focusedSid, query, "replace") : false
              )}
              onRemoveQueued={(query) => {
                if (focusedSid) cancelQueuedQuery(focusedSid, query);
              }}
              onTakebackQueued={(query) => {
                if (!focusedSid) return;
                if (query.queueState === "queued") inspectQueuedQuery(focusedSid, query, false, true);
                else {
                  cancelQueuedQuery(focusedSid, query);
                  setQueuedTakeback(query);
                }
              }}
              onInspectQueued={(query, actions) => {
                if (focusedSid) inspectQueuedQuery(focusedSid, query, actions);
              }}
              onSetModel={setModel}
              onSetEffort={setEffort}
              onSetPerm={setPerm}
              onClear={() => startNewChat()}
              onContext={requestContext}
              onDiff={() => getDiff("")}
              onPreview={previewMarkdown}
              onOpenExtensions={(kind) => {
                setCapabilitiesKind(kind);
                setCapabilitiesOpen(true);
                setCapabilitiesLoading(true);
                requestSkillCatalog({
                  key: focusedSkillCatalogKey,
                  engine: focusedEngine,
                  space,
                  cwd: capabilityCwd,
                  skillsOnly: false,
                  dshSessionId: focusedEngine === "dsh" ? focusedSid : null,
                }, true);
              }}
              contextReport={rt.contextReport}
              contextError={rt.contextError}
            />
          </>
        )}
      </section>
      {/* Shared right slot: diff (and previews) take turns. */}
      {(() => {
        if (state.artifact)
          return <Suspense fallback={
            <div className="artifact-panel empty" role="status">
              <div className="spinner" aria-hidden="true" />
              <p className="loading-tx">加载预览…</p>
            </div>
          }>
            <ArtifactPanel artifact={state.artifact}
              onRefresh={previewArtifactFile}
              onOpenFile={previewArtifactFile} onLoadPreviewAsset={loadPreviewAsset}
              onAuthorizePreview={authorizePreview}
              onSaveMarkdown={saveMarkdown} onDirtyChange={setArtifactDirty}
              onClose={() => dispatch({ type: "clear_artifact" })} />
          </Suspense>;
        return null;
      })()}
      <QueuedQueryDialog
        key={queuedQueryEditor
          ? `${queuedQueryEditor.sid}:${queuedQueryEditor.msgId}`
          : "closed"}
        editor={queuedQueryEditor}
        onClose={() => {
          if (!queuedQueryEditor?.saving) setQueuedQueryEditor(null);
        }}
        onBeginEdit={() => {
          const editor = queuedQueryEditor;
          if (!editor || editor.state !== "queued" || !queuedEditHoldSupported) return;
          const requestId = wsRef.current?.sendGetQueuedQueryTo(editor.sid, editor.msgId, "begin") ?? null;
          setQueuedQueryEditor(current => current ? {...current, editingHeld: requestId !== null,
            loading: requestId !== null, detailRequestId: requestId, prompt: null,
            error: requestId ? null : "编辑请求未发送，请等待连接恢复。"} : null);
        }}
        canAttach={queuedAttachmentsSupported || queuedQueryEditor?.state === "failed"}
        onTakeback={(prompt, images, files) => {
          const editor = queuedQueryEditor;
          if (!editor || !editor.takeback) return;
          cancelQueuedQuery(editor.sid, {
            msg_id: editor.msgId, prompt, images, files,
            queueState: editor.state, queueKind: editor.kind ?? undefined,
          });
          setQueuedTakeback({ msg_id: editor.msgId, prompt, images, files });
          setQueuedQueryEditor(null);
        }}
        onSave={(prompt, images, files) => updateQueuedQuery(prompt, false, images, files)}
        onStartNow={prompt => updateQueuedQuery(prompt, true)}
        canStartNow={!!(queuedQueryEditor && state.runtimes[queuedQueryEditor.sid]?.queuedStartNowSupported)}
        onRemove={() => {
          const editor = queuedQueryEditor;
          if (!editor) return;
          const runtime = stateRef.current.runtimes[editor.sid];
          const query = [...(runtime?.queue ?? []), ...(runtime?.failedDeferred ?? []),
            ...(runtime?.pendingSend ? [runtime.pendingSend] : [])].find(item => item.msg_id === editor.msgId);
          if (query) cancelQueuedQuery(editor.sid, query);
          setQueuedQueryEditor(null);
        }}
        onRetry={retryQueuedQuery} />
      {rt.pendingQuestion && (
        <QuestionSheet
          key={rt.pendingQuestion.ask_id}
          header={rt.pendingQuestion.header}
          question={rt.pendingQuestion.question}
          options={rt.pendingQuestion.options}
          allowText={rt.pendingQuestion.allow_text}
          secret={rt.pendingQuestion.secret}
          multiSelect={rt.pendingQuestion.multi_select}
          busy={askAnswerPending === rt.pendingQuestion.ask_id}
          cancelling={askCancelPending === rt.pendingQuestion.ask_id}
          onCancel={() => {
            const question = rt.pendingQuestion;
            if (!focusedSid || !question) return;
            if (!wsRef.current?.sendInterruptTo(focusedSid)) {
              dispatch({ type: "command_error", detail: "停止未发送，请等连接恢复后重试。" });
              return;
            }
            setAskCancelPending(question.ask_id);
          }}
          onAnswer={(answer) => {
            const question = rt.pendingQuestion;
            if (!focusedSid || !question) return;
            if (!wsRef.current?.sendAnswerQuestion(
              focusedSid, question.ask_id, answer)) {
              dispatch({
                type: "command_error",
                detail: "回答未发送，请等连接恢复后重试。",
              });
              return;
            }
            // Do NOT clear the card here: the wrapper closes it with
            // ask_user_closed once the answer is really accepted. Clearing
            // optimistically is how a lost answer looked like a dead tap.
            setAskAnswerPending(question.ask_id);
          }}
          onSkip={() => {
            const question = rt.pendingQuestion;
            if (!focusedSid || !question) return;
            if (!wsRef.current?.sendSkipQuestion(
              focusedSid, question.ask_id)) {
              dispatch({
                type: "command_error",
                detail: "操作未发送，请等连接恢复后重试。",
              });
              return;
            }
            setAskAnswerPending(question.ask_id);
          }}
        />
      )}
      <CapabilitiesSheet open={capabilitiesOpen}
        engine={focusedEngine}
        activeKind={capabilitiesKind}
        readOnly={focusedEngine === "dsh"}
        report={capabilitiesByScope[focusedSkillCatalogKey] ?? null}
        loading={capabilitiesLoading}
        onKindChange={setCapabilitiesKind}
        onRefresh={() => {
          setCapabilitiesLoading(true);
          requestSkillCatalog({
            key: focusedSkillCatalogKey,
            engine: focusedEngine,
            space,
            cwd: capabilityCwd,
            skillsOnly: false,
            dshSessionId: focusedEngine === "dsh" ? focusedSid : null,
          }, true);
        }}
        onManagePlugin={(item, action) => {
          const verb = action === "install" ? "安装" : "卸载";
          if (!window.confirm(`${verb}插件「${item.name}」将修改本机 DSH 配置，确定继续吗？`)) return;
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEnginePlugin(
            focusedEngine, space, action, item.id, capabilityCwd);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
          });
        }}
        onManageSkill={(item: EngineCapabilityItem, action) => {
          const labels = { enable: "启用", disable: "停用", remove: "删除" } as const;
          if (!window.confirm(`${labels[action]} Skill「${item.name}」？${action === "remove" ? "删除会移动到本机可恢复回收目录。" : ""}`)) return;
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEngineSkill(
            focusedEngine, space, action, { skillId: item.id },
            capabilityCwd);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
          });
        }}
        onCreateSkill={(draft: SkillDraft) => {
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEngineSkill(
            focusedEngine, space, "create", draft,
            capabilityCwd);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
          });
        }}
        onRemoveHook={(item: EngineCapabilityItem) => {
          if (!window.confirm(`删除 Hook「${item.name}」？配置文件中的其他内容会原样保留。`)) return;
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEngineHook(
            focusedEngine, space, "remove", { hookId: item.id },
            capabilityCwd);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
          });
        }}
        onClose={() => setCapabilitiesOpen(false)} />
      <DeviceSheet open={deviceSheetOpen}
        currentId={machineId}
        devices={remoteDevices}
        pairing={devicePairing}
        onDevices={(nextDevices, nextPairing) => {
          setRemoteDevices(nextDevices);
          setDevicePairing(nextPairing);
        }}
        onSelect={(nextMachineId) => {
          cancelPendingNotificationTarget();
          if (nextMachineId !== machineId) setMachineId(nextMachineId);
          setDeviceSheetOpen(false);
        }}
        onClose={() => setDeviceSheetOpen(false)} />
    </div>
  );
}
