import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type PointerEvent,
  type CSSProperties,
  type SetStateAction,
} from "react";
import type {
  State, QueryImg, QueryFile, ContextReport,
  PermissionModeInfo,
  SessionControl, EngineCapabilityKind,
  Engine,
} from "../protocol";
import { presentLegacyExternalControl, presentSessionControl } from "../session-control-ui";
import type { ConnState } from "../ws";
import { Icon } from "../icons";
import {
  clientSlashesFor, slashToken,
  matchCommands, parseSlash,
  modelsFor, effortNameForDisplay, permsFor,
  permissionNameForDisplay, type Catalog,
} from "../data";
import { appearanceLayout, type Appearance } from "../appearance";
import { useDeviceLayout } from "../use-device-layout";
import { DraftTools } from "./DraftTools";
import { useClipboardSend } from "../use-clipboard-send";
import { cloneDraft, draftHasContent } from "../composer-drafts";
import { SessionSettingsSheet } from "./SessionSettingsSheet";
import { CommandSheet } from "./CommandSheet";
import { useMetadataPress } from "../use-metadata-press";
import { attachmentBytes, pickFiles } from "../img";
import { clipboardFiles, insertClipboardText } from "../clipboard-content";
import type { PendingQuery } from "../reducer";
import { canEnqueueQuery, type QueueCapacity } from "../runtime-drain";
import { ImeSubmitGuard } from "../ime-submit";
import {
  isComposerBusy,
  isInterruptSettling,
  isSettlingStopDisabled,
  type SendMode,
} from "../composer-submit";
import type { ComposerDraft, ComposerDraftStore } from "../composer-drafts";
import {
  composePastePrompt,
  LONG_PASTE_THRESHOLD,
  makeComposerPaste,
} from "../composer-pastes";
import { PendingImageAttachments } from "./PendingImageAttachments";
import { QueuedQueryChip } from "./QueuedQueryDialog";
import { PasteCards } from "./PasteCards";
import { revealTextareaCaret } from "../textarea-caret";

interface Props {
  appearance?: Appearance;
  draftLabel?: string;
  draftKey: string;
  draftStore: ComposerDraftStore;
  state: State;
  connState: ConnState;
  wrapperOnline: boolean;
  sendMode: SendMode;
  setSendMode: (m: SendMode) => void;
  queue: PendingQuery[];
  pendingSend: PendingQuery | null;
  failedDeferred: PendingQuery[];
  unconfirmedQueued: PendingQuery[];
  unconfirmedReplaceable: PendingQuery[];
  queueCapacity: QueueCapacity;
  replaceQueueCapacity: QueueCapacity;
  model: string;
  effort: string;
  perm: string;
  permOptions: PermissionModeInfo[] | null;
  control?: SessionControl | null;
  // A native CLI in the terminal owns this session and is appending to its
  // transcript. We mirror it live but must NOT write: a session has a single
  // owner, so sending from here would fork the conversation.
  external?: boolean;
  takeoverPending?: boolean;
  takeoverMessage?: string | null;
  engine?: Engine;
  catalog?: Catalog;   // engine-reported models/efforts; falls back to data.ts
  editPrompt: string | null;
  onEditConsumed: () => void;
  restoreQuery?: PendingQuery | null;
  onRestoreQueryConsumed?: () => void;
  onSendQuery: (prompt: string, images?: QueryImg[], files?: QueryFile[]) => boolean;
  onInterrupt: () => void;
  onOpenSessions?: () => void;
  navigationOpen?: boolean;
  questionOpen?: boolean;
  onEnqueue: (query: PendingQuery) => boolean;
  onSetPending: (query: PendingQuery) => boolean;
  onRemoveQueued: (query: PendingQuery) => void;
  onTakebackQueued: (query: PendingQuery) => void;
  onInspectQueued: (query: PendingQuery, actions?: boolean) => void;
  onSetModel: (model: string) => void;
  onSetEffort: (effort: string) => void;
  onSetPerm: (perm: string) => void;
  onClear: () => void;
  onContext: () => void;
  onDiff?: () => void;
  onPreview?: (path: string) => void;
  onOpenExtensions?: (kind: EngineCapabilityKind | "all") => void;
  contextReport: ContextReport | null;
  contextError?: string | null;
}

export function Composer(p: Props) {
  const designed = appearanceLayout(p.appearance) !== "classic";
  const [settingsOpen, setSettingsOpen] = useState(false);
  const layout=useDeviceLayout();
  const mobileLayout=layout.overlay;
  const [expanded, setExpanded] = useState(false);
  const [editorHeight, setEditorHeight] = useState(220);
  const [editorFocused, setEditorFocused] = useState(false);
  const editorBlurTimer = useRef<number | null>(null);
  const cancelEditorBlur = () => {
    if (editorBlurTimer.current !== null) window.clearTimeout(editorBlurTimer.current);
    editorBlurTimer.current = null;
  };
  useEffect(() => () => {
    if (editorBlurTimer.current !== null) window.clearTimeout(editorBlurTimer.current);
  }, []);
  const composerRef = useRef<HTMLDivElement>(null);
  const editorDragRef = useRef<{ y: number; height: number } | null>(null);
  const editPrompt = p.editPrompt;
  const onEditConsumed = p.onEditConsumed;
  const draftKeyRef = useRef(p.draftKey);
  const draftRevision=useRef(0);
  const [draft, setDraft] = useState<ComposerDraft>(
    () => p.draftStore.get(p.draftKey));
  const input = draft.input;
  const images = draft.images;
  const files = draft.files;
  const pastes = draft.pastes;
  const updateDraft = useCallback((
    update: (current: ComposerDraft) => ComposerDraft,
  ) => {
    const key = draftKeyRef.current;
    draftRevision.current++;
    setDraft((current) => {
      const next = update(current);
      p.draftStore.set(key, next);
      return next;
    });
  }, [p.draftStore]);
  const setInput = useCallback((next: SetStateAction<string>) => updateDraft((current) => ({
    ...current,
    input: typeof next === "function" ? next(current.input) : next,
  })), [updateDraft]);
  const setImages = useCallback((next: SetStateAction<QueryImg[]>) => updateDraft((current) => ({
    ...current,
    images: typeof next === "function" ? next(current.images) : next,
  })), [updateDraft]);
  const setFiles = useCallback((next: SetStateAction<QueryFile[]>) => updateDraft((current) => ({
    ...current,
    files: typeof next === "function" ? next(current.files) : next,
  })), [updateDraft]);
  const clearDraft = useCallback(() => {
    updateDraft(() => ({ input: "", images: [], files: [], pastes: [] }));
    setExpanded(false);
  }, [updateDraft]);
  // Only the modal pickers live in state now; the "/" command palette is a live
  // popover DERIVED from the composer text (no second input box).
  const { metadataPressRef, beginMetadataPress, activateMetadata } = useMetadataPress(mobileLayout);
  const [sheetKind, setSheetKind] = useState<"models" | "efforts" | "perms" | null>(null);
  const openPermissions = () => {
    setSheetKind("perms");
  };
  useEffect(() => {
    setSettingsOpen(false);
    setSheetKind(null);
    setCtxOpen(false);
  }, [p.appearance]);
  const [ctxOpen, setCtxOpen] = useState(false);
  const ctxWrapRef = useRef<HTMLDivElement>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | null>(null);
  const [importing, setImporting] = useState(false);
  const attachmentImportLock = useRef(false);
  const [dragDepth, setDragDepth] = useState(0);
  const dragOver = dragDepth > 0;
  const taRef = useRef<HTMLTextAreaElement>(null);
  const imeSubmitRef = useRef(new ImeSubmitGuard());
  // Touch keyboards (iOS/Android) report keyCode 229 at the IME composition
  // boundary even after the candidate was confirmed, which the desktop guard
  // would treat as "still composing" and swallow the Enter — forcing the user
  // to hunt the on-screen send button. Coarse pointers are virtually always
  // soft keyboards, so relax the legacy 229 guard there (isComposing stays the
  // authoritative signal).
  const touchInput=layout.touchInput;
  const buttonSendTimerRef = useRef<number | null>(null);
  const photoRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const pickFilesRef = useRef<(files: FileList | File[] | null) => Promise<void>>(
    async () => {});

  useLayoutEffect(() => {
    if (draftKeyRef.current === p.draftKey) return;
    draftKeyRef.current = p.draftKey;
    draftRevision.current++;
    const restored=p.draftStore.get(p.draftKey);
    setDraft(restored);
    cancelEditorBlur();
    setEditorFocused(false);
    setSheetKind(null);
    setSettingsOpen(false);
    metadataPressRef.current = null;
    setCtxOpen(false);
    setExpanded(false);
    setNotice(null);
    if (noticeTimer.current !== null) {
      window.clearTimeout(noticeTimer.current);
      noticeTimer.current = null;
    }
  }, [p.draftKey, p.draftStore, metadataPressRef]);
  const toggleExpanded = () => {
    const textarea = taRef.current;
    const start = textarea?.selectionStart ?? 0;
    const end = textarea?.selectionEnd ?? start;
    setExpanded((value) => !value);
    window.requestAnimationFrame(() => {
      textarea?.focus({ preventScroll: true });
      textarea?.setSelectionRange(start, end);
      if (textarea) revealTextareaCaret(textarea, start);
    });
  };
  const startEditorDrag = (event: PointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    editorDragRef.current = { y: event.clientY, height: editorHeight };
  };
  const moveEditorDrag = (event: PointerEvent<HTMLButtonElement>) => {
    const drag = editorDragRef.current;
    if (!drag) return;
    const available = window.visualViewport?.height ?? window.innerHeight;
    setEditorHeight(Math.max(96, Math.min(available * .6, drag.height + drag.y - event.clientY)));
  };
  // Context popover shares one anchor and closes together.
  useEffect(() => {
    if (!ctxOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (ctxWrapRef.current && !ctxWrapRef.current.contains(e.target as Node)) {
        setCtxOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); setCtxOpen(false); }
    };
    document.addEventListener("pointerdown", onDoc);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDoc);
      window.removeEventListener("keydown", onKey);
    };
  }, [ctxOpen]);

  useEffect(() => () => {
    if (buttonSendTimerRef.current !== null) {
      window.clearTimeout(buttonSendTimerRef.current);
    }
  }, []);

  const busy = isComposerBusy(p.state);
  const offline = !p.wrapperOnline || p.connState !== "connected";
  const legacyControl = !p.control && p.external
    ? presentLegacyExternalControl(
        p.engine ?? "dsh",
        !!p.takeoverPending,
        p.takeoverMessage,
      ) : null;
  const controlUi = p.control ? presentSessionControl(p.control) : legacyControl;
  // The connection and authoritative write state independently gate input.
  const locked = offline || !!controlUi?.locked;
  const hasText = input.trim().length > 0 || pastes.length > 0;
  const hasAttachments = images.length > 0 || files.length > 0;

  useEffect(() => {
    if (!locked && !p.questionOpen) return;
    setSheetKind(null);
    setSettingsOpen(false);
    metadataPressRef.current = null;
    setCtxOpen(false);
  }, [locked, p.questionOpen, metadataPressRef]);

  useEffect(() => {
    if (p.engine !== "dsh") return;
    setFiles([]);
    setCtxOpen(false);
    setSheetKind((kind) => kind === "perms" ? null : kind);
  }, [p.engine, setFiles]);

  // edit: refill the input box with a past prompt (user-bubble edit button)
  useEffect(() => {
    if (editPrompt != null) {
      updateDraft((current) => ({ ...current, input: editPrompt, pastes: [] }));
      onEditConsumed();
      setTimeout(() => taRef.current?.focus(), 0);
    }
  }, [editPrompt, onEditConsumed, updateDraft]);

  const restoreQuery = p.restoreQuery;
  const onRestoreQueryConsumed = p.onRestoreQueryConsumed;
  useEffect(() => {
    const query = restoreQuery;
    if (!query) return;
    updateDraft((current) => ({
      ...current,
      input: current.input.trim()
        ? `${current.input}\n\n${query.prompt}`
        : query.prompt,
      images: [...current.images, ...(query.images ?? [])],
      files: [...current.files, ...(query.files ?? [])],
    }));
    onRestoreQueryConsumed?.();
    window.setTimeout(() => taRef.current?.focus(), 0);
  }, [restoreQuery, onRestoreQueryConsumed, updateDraft]);

  const nativeTextareaSizing = typeof CSS !== "undefined"
    && CSS.supports?.("field-sizing", "content");
  const resetTaHeight = () => {
    if (!taRef.current) return;
    if (nativeTextareaSizing) taRef.current.style.removeProperty("height");
    else taRef.current.style.height = "auto";
  };
  const sizingProbeRef = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => () => { sizingProbeRef.current?.remove(); }, []);
  const measureDraftHeight = (textarea: HTMLTextAreaElement) => {
    // Measure off-flow: never collapse the live composer under the scroll
    // observers merely to discover how many lines the draft needs.
    const probe = sizingProbeRef.current ?? document.createElement("textarea");
    sizingProbeRef.current = probe;
    const css = window.getComputedStyle(textarea);
    probe.setAttribute("aria-hidden", "true");
    probe.tabIndex = -1;
    probe.style.cssText = "position:fixed;top:0;left:-10000px;height:0;min-height:0;max-height:none;visibility:hidden;pointer-events:none;overflow:hidden;resize:none;field-sizing:fixed;";
    for (const property of ["font", "line-height", "letter-spacing", "padding", "border", "box-sizing", "white-space", "word-break", "overflow-wrap"]) {
      probe.style.setProperty(property, css.getPropertyValue(property));
    }
    probe.style.width = `${textarea.getBoundingClientRect().width}px`;
    probe.value = textarea.value;
    if (!probe.isConnected) document.body.append(probe);
    const border = parseFloat(css.borderTopWidth) + parseFloat(css.borderBottomWidth);
    return probe.scrollHeight + (css.boxSizing === "border-box" ? border : -parseFloat(css.paddingTop) - parseFloat(css.paddingBottom));
  };
  const growTa = useCallback(() => {
    const textarea = taRef.current;
    if (!textarea) return;
    if (mobileLayout || designed) {
      if (expanded || (!editorFocused && mobileLayout)) textarea.style.removeProperty("height");
      else {
        // One native editor grows with the draft; leave enough room to read
        // the conversation even when a large dictation keyboard is showing.
        const viewportHeight = window.visualViewport?.height ?? window.innerHeight;
        const minimum = designed ? 44 : 60;
        const limit = Math.max(minimum, Math.min(designed ? 120 : 144, viewportHeight * (designed ? .24 : .28)));
        textarea.style.setProperty("height", `${Math.min(Math.max(minimum, measureDraftHeight(textarea)), limit)}px`, "important");
      }
      return;
    }
    if (nativeTextareaSizing) {
      if (textarea.style.height) textarea.style.removeProperty("height");
      return;
    }
    textarea.style.height = Math.min(measureDraftHeight(textarea), 132) + "px";
  }, [nativeTextareaSizing, mobileLayout, expanded, editorFocused, designed]);
  useLayoutEffect(() => {
    growTa();
  }, [growTa, input, p.draftKey]);
  useEffect(() => {
    const resize = () => growTa();
    window.visualViewport?.addEventListener("resize", resize);
    window.addEventListener("resize", resize);
    return () => {
      window.visualViewport?.removeEventListener("resize", resize);
      window.removeEventListener("resize", resize);
    };
  }, [growTa]);
  const focusTa = () => setTimeout(() => taRef.current?.focus(), 0);
  const flash = (msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2200);
  };

  useEffect(()=>p.draftStore.setLabel(p.draftKey,p.draftLabel ?? "会话草稿"),[p.draftStore,p.draftKey,p.draftLabel]);
  const {reading:clipboardReading,pasteAndSend}=useClipboardSend({
    key:p.draftKey,label:p.draftLabel ?? "会话草稿",store:p.draftStore,
    revision:()=>draftRevision.current,
    isEmpty:()=>!draftHasContent({...draft,input:taRef.current?.value ?? input}),
    blocked:locked || importing || !!p.questionOpen || !!p.navigationOpen,
    onReady:copied=>{updateDraft(() => copied);submitPrompt(copied.input, copied);},onNotice:flash,
  });

  const onInput = (v: string) => setInput(v);

  // Command palette = live suggestions derived from the input. A slash matches
  // cc-remote/native commands. Suggestions stop once arguments begin.
  const cmdToken = slashToken(input);
  const cmdMatches = cmdToken !== null
    ? matchCommands(cmdToken, p.engine) : [];
  const suggestionOpen = cmdMatches.length > 0;

  const onPickFiles = async (fl: FileList | File[] | null) => {
    if (attachmentImportLock.current) { flash("附件正在导入，请稍候"); return; }
    const targetDraftKey = draftKeyRef.current;
    const generation=p.draftStore.generation;
    attachmentImportLock.current = true; setImporting(true);
    try {
      const batch = await pickFiles(
        fl, images.length + files.length, attachmentBytes(images, files),
        { imagesOnly: p.engine === "dsh" || p.engine === "codex" });
      if(p.draftStore.generation!==generation)return;
      if (draftKeyRef.current === targetDraftKey) {
        if (batch.images.length) {
          setImages((previous) => [...previous, ...batch.images]);
        }
        if (batch.files.length) {
          setFiles((previous) => [...previous, ...batch.files]);
        }
      } else if (batch.images.length || batch.files.length) {
        const prior = p.draftStore.get(targetDraftKey);
        p.draftStore.set(targetDraftKey, {
          ...prior,
          images: [...prior.images, ...batch.images],
          files: [...prior.files, ...batch.files],
        });
      }
      if (batch.errors.length) flash(batch.errors.join("；"));
    } finally {
      attachmentImportLock.current = false;
      setImporting(false);
    }
  };
  pickFilesRef.current = onPickFiles;

  // Whole-window drag-drop overlay. The effect is refreshed with the current
  // attachment limits/import state so its async drop handler never uses a stale
  // count or appends after a send.
  useEffect(() => {
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types || []).includes("Files");
    const onEnter = (e: DragEvent) => { if (hasFiles(e)) setDragDepth((d) => d + 1); };
    const onLeave = (e: DragEvent) => { if (hasFiles(e)) setDragDepth((d) => Math.max(0, d - 1)); };
    const onOver = (e: DragEvent) => { if (hasFiles(e)) e.preventDefault(); };
    const onDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      setDragDepth(0);
      if (e.dataTransfer?.files?.length) void pickFilesRef.current(e.dataTransfer.files);
    };
    window.addEventListener("dragenter", onEnter);
    window.addEventListener("dragleave", onLeave);
    window.addEventListener("dragover", onOver);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onEnter);
      window.removeEventListener("dragleave", onLeave);
      window.removeEventListener("dragover", onOver);
      window.removeEventListener("drop", onDrop);
    };
  }, []);

  // Keep the native textarea for reliable selection/undo/IME. Large text is
  // retained privately by the draft and represented only by an editable card.
  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = clipboardFiles(e.clipboardData);
    const text = e.clipboardData.getData("text/plain");
    if (files.length) {
      e.preventDefault();
      setInput(insertClipboardText(e.currentTarget, text));
      void onPickFiles(files); return;
    }
    if (text.length <= LONG_PASTE_THRESHOLD) return;
    e.preventDefault();
    const textarea = e.currentTarget;
    const id = crypto.randomUUID();
    updateDraft((current) => ({
      ...current,
      pastes: [...current.pastes, makeComposerPaste(text, id)],
    }));
    window.setTimeout(() => textarea.focus(), 0);
  };

  // Send prompt text, honoring busy/queue/interrupt rules.
  const submitPrompt = (prompt: string, source: ComposerDraft = draft) => {
    if (importing) { flash("请等待附件导入完成"); return; }
    const composed = composePastePrompt(source.pastes, prompt);
    if (!composed.ok) {
      flash(`消息内容超过上限（最多 ${composed.maxChars.toLocaleString()} 个字符）`);
      return;
    }
    const expandedPrompt = composed.prompt;
    const {images, files} = source;
    const hasAttachments = images.length > 0 || files.length > 0;
    const query: PendingQuery = {
      prompt: expandedPrompt,
      images: images.length ? images : undefined,
      files: p.engine!=="dsh" && files.length ? files : undefined,
    };
    if (busy) {
      if (!expandedPrompt && !hasAttachments) return;
      if (!canEnqueueQuery(p.unconfirmedQueued, query, p.queueCapacity)) {
        flash("待发送已满（最多 32 条 / 64 MiB）");
        return;
      }
      if (p.onEnqueue(query)) { p.draftStore.archive(p.draftKey,{...source,input:prompt},"sent",p.draftLabel);clearDraft(); resetTaHeight(); }
      return;
    }
    if (!expandedPrompt && !hasAttachments) return;
    if (p.onSendQuery(
        expandedPrompt, images.length ? images : undefined, p.engine!=="dsh" && files.length ? files : undefined)) {
      p.draftStore.archive(p.draftKey,{...source,input:prompt},"sent",p.draftLabel);
      clearDraft(); resetTaHeight();
    }
  };

  // Client-side slash commands — never forwarded to the engine. "/model <id>"
  // passes the id straight to set_model (hidden models included).
  const runClientSlash = (slash: string, args: string) => {
    switch (slash) {
      case "model":
        if (args) { p.onSetModel(args); flash(`正在切换模型：${args}`); }
        else setSheetKind("models");
        break;
      case "permissions": openPermissions(); break;
      case "clear": p.onClear(); break;
      // open the popup too (not just fetch) — same as clicking the context ring.
      case "context":
        p.onContext();
        setCtxOpen(true);
        break;
      case "diff":
        if (args.trim()) { flash("/diff 不接受参数"); return; }
        p.onDiff?.();
        break;
      case "preview":
        if (!args) {
          setInput("/preview ");
          focusTa();
          return;
        }
        p.onPreview?.(args);
        break;
      case "extensions": p.onOpenExtensions?.("all"); break;
      case "skills": p.onOpenExtensions?.("skill"); break;
      case "plugins": p.onOpenExtensions?.("plugin"); break;
      case "apps": p.onOpenExtensions?.("app"); break;
      case "mcp": p.onOpenExtensions?.("mcp"); break;
      case "hooks": p.onOpenExtensions?.("hook"); break;
    }
    setInput(""); resetTaHeight();
  };

  // Pick a command from the palette. Client commands run now; unknown skills
  // fill the composer so the user can add args, then send.
  const pickCommand = (slash: string) => {
    if (clientSlashesFor(p.engine).has(slash)) { runClientSlash(slash, ""); focusTa(); return; }
    setInput("/" + slash + " ");
    focusTa(); growTa();
  };

  const send = (value = taRef.current?.value ?? input) => {
    if (locked || importing) return;
    const raw = value.trim();
    if (raw === "/") return;
    const parsed = parseSlash(raw);
    if (parsed && clientSlashesFor(p.engine).has(parsed.slash)) { runClientSlash(parsed.slash, parsed.args); return; }
    // DSH owns its own slash layer; everything else is sent verbatim.
    submitPrompt(raw);
  };

  const requestButtonSend = () => {
    const value=taRef.current?.value ?? input;
    if(!busy && !value.trim() && !hasAttachments && pastes.length===0
        && !imeSubmitRef.current.shouldCommitBeforeButtonSubmit()) {void pasteAndSend();return;}
    if(busy && !value.trim() && !hasAttachments && pastes.length===0)draftRevision.current++;
    if (buttonSendTimerRef.current !== null) return;
    buttonSendTimerRef.current = window.setTimeout(() => {
      buttonSendTimerRef.current = null;
      const buttonPrompt = taRef.current?.value ?? input;
      if (busy && !buttonPrompt.trim() && !hasAttachments && pastes.length === 0) {
        if (p.state === "running") p.onInterrupt();
        return;
      }
      send(buttonPrompt);
    }, 0);
  };

  const stopping = busy && !hasText && !hasAttachments;
  const interruptSettling = isInterruptSettling(p.state);
  const pasteMode=!busy && !hasText && !hasAttachments;
  const sendIcon = stopping ? "stop" : pasteMode ? "paste-send" : "send";
  const sendClass = "sendbtn" + (stopping ? " interrupt" : "") + (pasteMode ? " paste-mode" : "");
  const disabled = locked || importing || (pasteMode && clipboardReading)
    || isSettlingStopDisabled(p.state, hasText || hasAttachments);
  // Fall back to the raw id so a hidden model set via "/model <id>" shows its
  // actual id on the chip instead of a curated display name.
  const MODELS_E = modelsFor(p.engine, p.catalog);
  const PERMS_E = permsFor(p.engine, p.permOptions);
  const contextAvailable = p.contextReport?.available !== false;
  const model = p.model
    ? (MODELS_E.find((m) => m.id === p.model)
      || { id: p.model, name: p.model, ds: "", ic: "cpu" })
    : null;
  const effortName = effortNameForDisplay(p.effort);
  const perm = p.perm
    ? (PERMS_E.find((x) => x.id === p.perm)
      || { id: p.perm, name: p.perm, short: p.perm, ds: "", ic: "shield" })
    : null;
  const modeLabel = perm?.short ?? perm?.name
    ?? permissionNameForDisplay(p.perm) ?? "权限读取中";
  const modeCls = perm?.id === "plan" ? " plan" : perm?.danger ? " danger" : "";

  const inputControl = (placeholder: string) => (
    <textarea
      key="composer-input"
      ref={taRef}
      rows={1}
      aria-label="消息输入框"
      value={input}
      enterKeyHint={mobileLayout || expanded ? "enter" : undefined}
      placeholder={mobileLayout && !expanded && !editorFocused ? "" : importing ? "正在安全导入附件…"
        : offline ? "机器离线 — 等待重连…"
        : (controlUi?.placeholder
          ?? (busy
            ? "输入下一条消息，当前任务结束后发送…"
            : placeholder))}
      disabled={locked}
      onFocus={() => { cancelEditorBlur(); setEditorFocused(true); }}
      onBlur={() => {
        cancelEditorBlur();
        // iOS can blur on touchend before dispatching click. Keep the tapped
        // control in place until that click finishes, then collapse the editor.
        editorBlurTimer.current = window.setTimeout(() => {
          editorBlurTimer.current = null;
          if (document.activeElement !== taRef.current) setEditorFocused(false);
        }, 150);
      }}
      onChange={(e) => onInput(e.target.value)}
      onCompositionStart={() => imeSubmitRef.current.startComposition()}
      onCompositionEnd={(e) => {
        imeSubmitRef.current.endComposition();
        onInput(e.currentTarget.value);
      }}
      onPaste={onPaste}
      onKeyDown={(e) => {
        if (e.key === "Escape" && suggestionOpen) {
          setInput(""); resetTaHeight(); return;
        }
        if ((touchInput || expanded) && !(e.metaKey || e.ctrlKey)) return;
        if (e.nativeEvent.isComposing || imeSubmitRef.current.shouldCommitBeforeButtonSubmit()) return;
        if (!imeSubmitRef.current.shouldSubmitKey({
          key: e.key,
          shiftKey: e.shiftKey,
          isComposing: e.nativeEvent.isComposing,
          keyCode: e.nativeEvent.keyCode,
        }, { mobile: touchInput })) return;
        e.preventDefault();
        if (cmdMatches.length > 0 && cmdToken) {
          pickCommand(cmdMatches[0].slash); return;
        }
        if (cmdToken === "") return;
        send(e.currentTarget.value);
      }}
    />
  );

  const sendControl = (
    <button className={sendClass}
      onPointerDown={(event) => {
        if (mobileLayout) event.preventDefault();
        if (imeSubmitRef.current.shouldCommitBeforeButtonSubmit()) taRef.current?.blur();
      }}
      onClick={requestButtonSend}
      disabled={disabled}
      aria-label={interruptSettling && stopping ? "正在停止" : stopping ? "停止" : pasteMode ? (clipboardReading ? "正在读取剪贴板" : "粘贴并发送") : "发送"}
      title={pasteMode ? "读取剪贴板并直接发送" : stopping ? "停止当前任务" : "发送消息"}>
      {clipboardReading && pasteMode ? <span className="clipboard-reading"/> : <Icon name={sendIcon} size={19} />}
    </button>
  );

  return (
    <div className={"composer" + (mobileLayout ? " floating-composer" : "")
      + (designed ? " designed-composer" : "")
      + (expanded ? " composer-expanded" : "") + (editorFocused ? " editor-focused" : "")}
      ref={composerRef}
      onPointerDownCapture={() => { metadataPressRef.current = null; }}
      onPointerCancelCapture={() => { metadataPressRef.current = null; }}
      style={{ "--composer-editor-height": `${editorHeight}px` } as CSSProperties}>
      <div className="composer-in">
        {suggestionOpen && (
          <div className="cmd-pop" role="listbox" aria-label="命令"
            data-lock-horizontal-swipe="true">
            {cmdMatches.map((c) => (
              <button key={c.slash} type="button" className="cmd" onClick={() => pickCommand(c.slash)}>
                <span className="cmd-ic"><Icon name={c.ic} size={17} /></span>
                <span className="cmd-tx">
                  <span className="cmd-nm"><span className="slash">/{c.slash}</span></span>
                  <span className="cmd-ds">{c.name} — {c.ds}</span>
                </span>
                <span className="cmd-kbd">↵</span>
              </button>
            ))}
          </div>
        )}
        {notice && <div className="composer-notice">{notice}</div>}
        {p.control?.control_mode === "desktop" && controlUi?.locked && (
          <div className="composer-control-note" role="status">
            <Icon name="branch" size={14} />
            <span>{controlUi.detail}</span>
          </div>
        )}
        {(p.queue.length > 0 || p.pendingSend || p.failedDeferred.length > 0) && (
          <div className="queued show">
            {[
              ...(p.pendingSend ? [p.pendingSend] : []),
              ...p.queue,
              ...p.failedDeferred,
            ].map((m, i) => (
              <QueuedQueryChip query={m} key={m.msg_id ?? i}
                onOpen={p.onInspectQueued}
                onActions={query => p.onInspectQueued(query, true)}
              onTakeback={() => p.onTakebackQueued(m)}
                onRemove={() => p.onRemoveQueued(m)} />
            ))}
          </div>
        )}

        {(hasAttachments || pastes.length > 0) && (
          <div className="attach show">
            <PendingImageAttachments key={p.draftKey} images={images}
              onRemove={(index) => setImages((previous) =>
                previous.filter((_, candidate) => candidate !== index))} />
            {files.map((f, i) => (
              <span key={i} className="attach-file">
                <Icon name="read" size={14} />
                <span className="attach-fn">{f.filename}</span>
                <button className="attach-x" onClick={() => setFiles(files.filter((_, j) => j !== i))} aria-label="移除"><Icon name="close" size={12} /></button>
              </span>
            ))}
            <PasteCards pastes={pastes}
              onChange={(nextPastes) => updateDraft((current) => ({
                ...current, pastes: nextPastes,
              }))} />
          </div>
        )}

        <input ref={photoRef} type="file" accept="image/*" multiple hidden
          aria-label="添加照片"
          onChange={(e) => { void onPickFiles(e.target.files); e.target.value = ""; }} />
        <input ref={fileRef} type="file" multiple hidden aria-label="添加文件"
          onChange={(e) => { void onPickFiles(e.target.files); e.target.value = ""; }} />

        <div className="inrow">
          {(mobileLayout || designed) && <div className="composer-edit-head">
            <span>草稿 {input.length.toLocaleString()} 字</span>
            <button type="button" className="composer-grip" aria-label="拖动调整输入区高度"
              onPointerDown={startEditorDrag} onPointerMove={moveEditorDrag}
              onPointerUp={() => { editorDragRef.current = null; }}
              onPointerCancel={() => { editorDragRef.current = null; }}><i /></button>
            <button type="button" className="composer-size-toggle iconbtn" aria-label={expanded ? "收拢输入区" : "展开输入区"}
              onPointerDown={(e) => e.preventDefault()} onClick={toggleExpanded}><Icon name={expanded ? "collapse" : "expand"} size={17} /></button>
          </div>}
          <DraftTools store={p.draftStore} draftKey={p.draftKey} label={p.draftLabel ?? "会话草稿"}
            draft={draft} busy={busy} disabled={importing} reading={clipboardReading} onNotice={flash} readOnly={locked || !!p.questionOpen}
            onPhoto={()=>photoRef.current?.click()} onPasteSend={()=>{void pasteAndSend();}}
            onClear={()=>{clearDraft();resetTaHeight();}}
            onRestore={restored=>{updateDraft(()=>cloneDraft(restored));focusTa();}} />
          {designed && <button type="button" className="composer-config-trigger" aria-label="会话配置"
            onPointerDown={beginMetadataPress}
            onClick={event => activateMetadata(event, () => { setSettingsOpen(true); setCtxOpen(false); })}
            disabled={locked}>
            <Icon name="settings" size={18} />
            <span>{model?.name ?? "本机模型"}{effortName ? ` · ${effortName}` : ""}</span>
            <Icon name="chev" size={12} />
          </button>}
          {inputControl("输入消息或 DSH 原生命令")}
          {mobileLayout && <span className={"composer-preview" + (input.trim() ? "" : " empty")} aria-hidden="true">
            {input.slice(0,160).trim().replace(/\s+/g, " ") || "输入指令…"}
          </span>}
          {(mobileLayout || designed) && p.onOpenSessions && <button type="button" className="composer-sessions iconbtn"
            aria-label="打开会话" onPointerDown={(e) => e.preventDefault()}
            onClick={() => { setCtxOpen(false); p.onOpenSessions?.(); }}>
            <Icon name={designed ? "message-square" : "conversations"} size={19} sw={1.5} />
          </button>}
          {sendControl}
        </div>
        {!designed && <div className="hint">
          <button
            type="button"
            className={"hint-mode" + modeCls}
            onPointerDown={beginMetadataPress}
            onClick={event => activateMetadata(event, openPermissions)}
            disabled={locked || PERMS_E.length === 0}
            title="点击切换权限模式"
          >
            {modeLabel}
            <span className="hint-mode-ch">▾</span>
          </button>
          <span className="hint-kbds">{touchInput
            ? <><kbd>回车</kbd> 换行 · 点箭头发送</>
            : <><kbd>Enter</kbd> 发送 · <kbd>Shift+Tab</kbd> 切模式 · <kbd>/</kbd> 命令</>}</span>
          <div className="hint-right" ref={ctxWrapRef}>
            <button className="hint-ctl" onClick={event => activateMetadata(event, () => setSheetKind("models"))}
              onPointerDown={beginMetadataPress}
              disabled={locked}
              aria-label="选择模型" title="选择模型">{model?.name ?? "模型读取中"}</button>
            <button className="hint-ctl" onClick={event => activateMetadata(event, () => setSheetKind("efforts"))}
              onPointerDown={beginMetadataPress}
              disabled={locked}
              aria-label="思考强度" title="思考强度">{effortName ?? "强度读取中"}</button>
            <button
              className={"hint-ring" + (contextAvailable ? "" : " unavailable")}
              aria-label="上下文占用"
              title="上下文占用"
              onPointerDown={beginMetadataPress}
              onClick={event => activateMetadata(event, () => {
                p.onContext();
                setCtxOpen((o) => !o);
              })}
            >
              <svg viewBox="0 0 36 36" width="20" height="20" aria-hidden="true">
                <circle className="hr-track" cx="18" cy="18" r="15" />
                <circle
                  className="hr-fill"
                  cx="18" cy="18" r="15"
                  strokeDasharray="94.25"
                  strokeDashoffset={94.25 * (1 - Math.min(
                    contextAvailable ? p.contextReport?.percentage ?? 0 : 0, 100) / 100)}
                  transform="rotate(-90 18 18)"
                />
              </svg>
            </button>
            {ctxOpen && (
              <div className="ctx-pop" role="dialog" aria-label="上下文占用">
                {p.contextError ? (
                  <div className="ctx-pop-loading" role="alert">{p.contextError}</div>
                ) : p.contextReport?.available === false ? (
                  <div className="ctx-pop-loading">尚未收到上下文用量；完成一次模型回合后会自动更新。</div>
                ) : p.contextReport ? (
                  <>
                    <div className="ctx-pop-row">
                      <span>上下文窗口</span>
                      <span className="ctx-pop-nums">
                        {p.contextReport.total_tokens.toLocaleString()} / {p.contextReport.max_tokens.toLocaleString()} ({p.contextReport.percentage.toFixed(0)}%)
                      </span>
                    </div>
                    <div className="ctx-pop-bar"><i style={{ width: `${Math.min(p.contextReport.percentage, 100)}%` }} /></div>
                    {p.contextReport.categories.length > 0 && (
                      <div className="ctx-pop-cats">
                        {p.contextReport.categories.map((c, i) => (
                          <div className="ctx-pop-cat" key={i}>
                            <span className="ctx-cat-dot" style={{ background: c.color }} />
                            <span className="ctx-pop-cat-name">{c.name}</span>
                            <span className="ctx-pop-cat-tok">{c.tokens.toLocaleString()}</span>
                          </div>
                        ))}
                      </div>
                    )}
                    {p.contextReport.is_auto_compact_enabled && <div className="ctx-pop-auto">autocompact 已启用</div>}
                    <div className="ctx-pop-foot">{p.contextReport.model || ""}</div>
                  </>
                ) : (
                  <div className="ctx-pop-loading">读取上下文占用…</div>
                )}
              </div>
            )}
          </div>
        </div>}
      </div>

      <SessionSettingsSheet open={designed && settingsOpen} onClose={() => setSettingsOpen(false)}
        rows={[
          {label:"模型",value:model?.name ?? "本机模型",icon:"cpu",actionLabel:"选择模型",disabled:locked,
            onClick:() => {setSettingsOpen(false);setSheetKind("models");}},
          {label:"思考强度",value:effortName ?? "由本机管理",icon:"gauge3",actionLabel:"思考强度",disabled:locked,
            onClick:() => {setSettingsOpen(false);setSheetKind("efforts");}},
          {label:"权限",value:modeLabel,icon:"shield",actionLabel:"点击切换权限模式",disabled:locked || PERMS_E.length === 0,
            onClick:() => {setSettingsOpen(false);setSheetKind("perms");}},
          {label:"上下文",value:contextAvailable ? `${p.contextReport?.percentage.toFixed(0)}% 已用` : "查看用量",
            icon:"history",actionLabel:"上下文占用",onClick:p.onContext},
        ]}
        note={p.contextError ?? (p.contextReport && contextAvailable
          ? `${p.contextReport.total_tokens.toLocaleString()} / ${p.contextReport.max_tokens.toLocaleString()} tokens`
          : "模型、强度和权限由当前本机 DSH 提供")} />

      <CommandSheet
        open={sheetKind !== null}
        kind={sheetKind ?? "models"}
        engine={p.engine}
        catalog={p.catalog}
        onClose={() => setSheetKind(null)}
        currentModel={p.model}
        onPickModel={(m) => { p.onSetModel(m); setSheetKind(null); }}
        currentEffort={p.effort}
        onPickEffort={(ef) => { p.onSetEffort(ef); setSheetKind(null); }}
        currentPerm={p.perm}
        permOptions={p.permOptions}
        onPickPerm={(perm) => {
          p.onSetPerm(perm);
          setSheetKind(null);
        }}
      />

      {dragOver && (
        <div className="drop-overlay" aria-hidden="true">
          <div className="drop-card">
            <span className="dc-ic"><Icon name="plus" size={36} /></span>
            <div className="dc-tx">拖拽图片到此处发送</div>
            <div className="dc-sub">DeepSeek Harness 当前仅接收图片附件</div>
          </div>
        </div>
      )}
    </div>
  );
}
