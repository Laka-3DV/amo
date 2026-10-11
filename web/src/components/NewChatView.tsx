// Empty-state "new chat" page: a centered composer (a la DeepSeek Harness)
// with a working directory, optional model/effort overrides, and attachments.
// A null override is intentional: the wrapper/engine keeps its own local default.
/* oxlint-disable react/only-export-components */
import { useEffect, useLayoutEffect, useRef, useState, type ClipboardEvent } from "react";
import { Icon } from "../icons";
import {
  effortsFor, modelsFor, type Catalog, type Effort, type Model,
} from "../data";
import { attachmentBytes, pickFiles } from "../img";
import { clipboardFiles, insertClipboardText } from "../clipboard-content";
import type { DshPresetInfo, Engine, EngineInfo, QueryImg, QueryFile } from "../protocol";
import { ImeSubmitGuard } from "../ime-submit";
import { PendingImageAttachments } from "./PendingImageAttachments";
import {
  composePastePrompt,
  LONG_PASTE_THRESHOLD,
  makeComposerPaste,
  type ComposerPaste,
} from "../composer-pastes";
import { PasteCards } from "./PasteCards";
import { appearanceLayout, type Appearance } from "../appearance";
import { useDeviceLayout } from "../use-device-layout";
import { DraftTools } from "./DraftTools";
import { useClipboardSend } from "../use-clipboard-send";
import { ComposerDraftStore, draftHasContent, sameDraftScope, type ComposerDraft } from "../composer-drafts";
import { SessionSettingsSheet } from "./SessionSettingsSheet";
import { PickerSheet } from "./PickerSheet";
import { useMetadataPress } from "../use-metadata-press";

export interface NewChatCatalogRequest {
  engine: Engine;
  cwd?: string;
}

/** Catalog reads are scoped like the session they describe. */
export function newChatCatalogRequest(
  engine: Engine,
): NewChatCatalogRequest {
  return { engine };
}

export interface NewChatLocalDefaults {
  model: string | null;
  effort: string | null;
}

/** Cwd-aware defaults are presentation metadata for that exact Code
 * directory only. The selected overrides themselves remain null until the user
 * picks. */
export function resolveNewChatLocalDefaults(
  engine: Engine,
  modelDefaults: Record<string, string>,
  effortDefaults: Record<string, string>,
  catalogScopeKey: string = engine,
): NewChatLocalDefaults {
  return {
    model: modelDefaults[catalogScopeKey] ?? null,
    effort: effortDefaults[catalogScopeKey] ?? null,
  };
}

/** Keep a user's explicit effort only when the newly selected model supports
 * it. Unknown/default targets fail safe to null; we never invent a highest
 * effort on the user's behalf. */
export function compatibleNewChatEffort(
  engine: Engine,
  nextModel: string | null,
  currentEffort: string | null,
  catalog: Catalog,
  localDefaultModel: string | null,
): string | null {
  if (!currentEffort) return null;
  const effectiveModel = nextModel ?? localDefaultModel;
  if (!effectiveModel) return null;
  if (!modelsFor(engine, catalog).some(
    (candidate) => candidate.id === effectiveModel,
  )) return null;
  return effortsFor(engine, effectiveModel, catalog).some(
    (candidate) => candidate.id === currentEffort,
  ) ? currentEffort : null;
}

export function reconcileNewChatSelection(
  engine: Engine,
  model: string | null,
  effort: string | null,
  catalog: Catalog,
  localDefaultModel: string | null,
): { model: string | null; effort: string | null } {
  if (model && !modelsFor(engine, catalog).some(
    (candidate) => candidate.id === model,
  )) {
    // A fallback model can disappear when the authoritative, entitlement-
    // filtered catalog arrives. Clear both overrides instead of submitting a
    // now-inaccessible model with a stale effort.
    return { model: null, effort: null };
  }
  return {
    model,
    effort: compatibleNewChatEffort(
      engine, model, effort, catalog, localDefaultModel),
  };
}

export function newChatEfforts(
  engine: Engine,
  effectiveModel: string | null,
  catalog: Catalog,
): Effort[] {
  return effortsFor(engine, effectiveModel, catalog);
}

interface Props {
  appearance?: Appearance;
  draftStore: ComposerDraftStore;
  draftKey: string;
  cwd: string;
  engines?: EngineInfo[];
  onPickEngine?: (engine: Engine) => void;
  engine?: Engine;  // which backend this new chat will use
  catalog?: Catalog;
  model?: string | null;
  effort?: string | null;
  defaultModel?: string | null;
  defaultEffort?: string | null;
  dshPresets?: DshPresetInfo[];
  defaultDshPresetId?: string | null;
  dshAgentPreset?: string | null;
  autoFocus?: boolean;
  createError?: string | null;
  onOpenSessions?: () => void;
  onPickCwd: () => void;  // open the directory picker
  onPickModel?: (model: string | null) => void;
  onPickEffort?: (effort: string | null) => void;
  onPickDshPreset?: (presetId: string | null) => void;
  onSend: (prompt: string, images?: QueryImg[], files?: QueryFile[]) => boolean;
}

interface SelectorOption {
  id: string | null;
  name: string;
  description: string;
  icon: string;
}

function NewChatSelectorSheet({
  open, kind, options, current, onClose, onPick,
}: {
  open: boolean;
  kind: "models" | "efforts";
  options: SelectorOption[];
  current: string | null;
  onClose: () => void;
  onPick: (value: string | null) => void;
}) {
  const title = kind === "models" ? "选择模型" : "选择思考强度";
  return (
    <PickerSheet open={open} title={title} onClose={onClose}>
      {options.map((option) => (
        <button key={option.id ?? "__local_default__"}
          className={"cmd" + (option.id === current ? " sel" : "")}
          onClick={() => onPick(option.id)}>
          <span className="cmd-ic"><Icon name={option.icon} size={17} /></span>
          <span className="cmd-tx">
            <span className="cmd-nm">{option.name}</span>
            <span className="cmd-ds">{option.description}</span>
          </span>
          {option.id === current
            ? <span className="cmd-check"><Icon name="check" size={19} /></span>
            : <span className="cmd-kbd" />}
        </button>
      ))}
    </PickerSheet>
  );
}

function displayModel(
  id: string | null | undefined, models: Model[],
): string | null {
  if (!id) return null;
  return models.find((candidate) => candidate.id === id)?.name ?? id;
}

function displayEffort(
  id: string | null | undefined, efforts: Effort[],
): string | null {
  if (!id) return null;
  return efforts.find((candidate) => candidate.id === id)?.name ?? id;
}

export function NewChatView({ cwd, appearance, draftStore, draftKey,
  engine = "dsh", engines = [], onPickEngine,
  catalog = {}, model = null, effort = null,
  defaultModel = null, defaultEffort = null, autoFocus = true, createError,
  dshPresets = [], defaultDshPresetId = null, dshAgentPreset = null,
  onPickCwd, onOpenSessions,
  onPickModel, onPickEffort, onPickDshPreset,
  onSend }: Props) {
  const designed = appearanceLayout(appearance) !== "classic";
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const initialDraft=useRef(draftStore.get(draftKey));
  const revision=useRef(0);
  const [text, setText] = useState(initialDraft.current.input);
  const touchInput=useDeviceLayout().touchInput;
  const { metadataPressRef, beginMetadataPress, activateMetadata } = useMetadataPress(touchInput);
  const [images, setImages] = useState<QueryImg[]>(initialDraft.current.images);
  const [files, setFiles] = useState<QueryFile[]>(initialDraft.current.files);
  const [pastes, setPastes] = useState<ComposerPaste[]>(initialDraft.current.pastes);
  const [importing, setImporting] = useState(false);
  const attachmentImportLock = useRef(false);
  const [creating, setCreating] = useState(false);
  const [notice,setNotice]=useState("");
  const currentKey=useRef(draftKey);
  const [draftScope,setDraftScope]=useState(draftKey);
  const liveDraft=useRef<ComposerDraft>({input:text,images,files,pastes});
  liveDraft.current={input:text,images,files,pastes};
  const [sheetKind, setSheetKind] =
    useState<"models" | "efforts" | null>(null);
  const photoRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const imeSubmitRef = useRef(new ImeSubmitGuard());
  const buttonSendTimerRef = useRef<number | null>(null);

  useLayoutEffect(()=>{
    if(currentKey.current===draftKey)return;
    const oldKey=currentKey.current;
    let restored=draftStore.get(draftKey);
    // Choosing another directory belongs to this same new-chat intent. Carry
    // the prompt while preserving any prior destination draft as a version.
    if(sameDraftScope(oldKey,draftKey) && draftHasContent(liveDraft.current)) {
      draftStore.archive(oldKey,liveDraft.current,"saved");
      draftStore.archive(draftKey,restored,"replaced",cwd);
      restored=liveDraft.current;draftStore.delete(oldKey);draftStore.set(draftKey,restored);
    }
    currentKey.current=draftKey;revision.current++;setDraftScope(draftKey);
    setText(restored.input);setImages(restored.images);setFiles(restored.files);setPastes(restored.pastes);
  },[draftKey,draftStore,cwd]);
  useEffect(()=>{
    if(draftScope!==draftKey)return;
    draftStore.setLabel(draftKey,cwd);
    if(creating){draftStore.delete(draftKey);return;}
    draftStore.set(draftKey,{input:text,images,files,pastes});
  },[draftStore,draftKey,draftScope,cwd,text,images,files,pastes,creating]);
  useEffect(()=>{if(!notice)return;const timer=setTimeout(()=>setNotice(""),4000);return()=>clearTimeout(timer);},[notice]);
  useEffect(() => { setSettingsOpen(false); setSheetKind(null); }, [appearance]);
  useLayoutEffect(() => {
    if (!designed) { taRef.current?.style.removeProperty("height"); return; }
    const grow = () => {
      const textarea = taRef.current;
      if (!textarea) return;
      textarea.style.height = "0px";
      const visibleHeight = window.visualViewport?.height ?? window.innerHeight;
      const limit = expanded ? Math.min(300, visibleHeight * .6) : Math.min(120, visibleHeight * .24);
      textarea.style.height = `${expanded ? Math.max(72, limit) : Math.max(44, Math.min(textarea.scrollHeight, limit))}px`;
    };
    grow();
    window.visualViewport?.addEventListener("resize", grow);
    window.addEventListener("resize", grow);
    return () => {
      window.visualViewport?.removeEventListener("resize", grow);
      window.removeEventListener("resize", grow);
    };
  }, [designed, text, expanded]);

  useEffect(() => {
    if (createError) setCreating(false);
  }, [createError]);

  useEffect(() => {
    if (engine === "dsh") setFiles([]);
  }, [engine]);

  useEffect(() => () => {
    if (buttonSendTimerRef.current !== null) {
      window.clearTimeout(buttonSendTimerRef.current);
    }
  }, []);

  const hasAttachments = images.length > 0 || files.length > 0;
  const effectiveDshPresetId = dshAgentPreset ?? defaultDshPresetId;
  const selectedDshPreset = engine === "dsh" && effectiveDshPresetId
    ? dshPresets.find((preset) => preset.id === effectiveDshPresetId) ?? null
    : null;
  const selectedDshPresetMissing = engine === "dsh"
    && !!dshAgentPreset && selectedDshPreset === null;
  const selectedDshPresetWarning = selectedDshPresetMissing
    ? "所选 DSH Agent Preset 已不可用，请重新选择。"
    : selectedDshPreset?.broken ?? null;
  const canSend = (text.trim().length > 0 || hasAttachments || pastes.length > 0)
    && !creating && !importing
    && !selectedDshPresetMissing && !selectedDshPreset?.broken;
  const modelList = modelsFor(engine, catalog);
  const effectiveModel = model ?? defaultModel;
  const effortList = newChatEfforts(engine, effectiveModel, catalog);
  const localModelName = displayModel(defaultModel, modelList);
  const localEffortName = model === null
    ? displayEffort(defaultEffort, effortList) : null;
  const modelLabel = model
    ? displayModel(model, modelList) ?? model
    : localModelName ? `本机默认 · ${localModelName}` : "本机默认";
  const effortLabel = effort
    ? displayEffort(effort, effortList) ?? effort
    : localEffortName ? `默认 · ${localEffortName}` : "默认";
  const modelOptions: SelectorOption[] = [{
    id: null,
    name: "本机默认",
    description: localModelName
      ? `使用本机配置 · ${localModelName}`
      : "不发送模型覆盖，使用本机配置",
    icon: "cpu",
  }, ...modelList.map((candidate) => ({
    id: candidate.id,
    name: candidate.name,
    description: candidate.ds,
    icon: candidate.ic,
  }))];
  const effortOptions: SelectorOption[] = [{
    id: null,
    name: "默认",
    description: localEffortName
      ? `使用本机配置 · ${localEffortName}`
      : "不发送强度覆盖，使用所选模型或本机配置",
    icon: "gauge3",
  }, ...effortList.map((candidate) => ({
    id: candidate.id,
    name: candidate.name,
    description: candidate.ds,
    icon: candidate.ic,
  }))];

  const onPick = async (fl: FileList | File[] | null) => {
    if (attachmentImportLock.current) return;
    const targetKey=draftKey;
    const generation=draftStore.generation;
    attachmentImportLock.current = true; revision.current++;setImporting(true);
    try {
      const batch = await pickFiles(
        fl, images.length + files.length, attachmentBytes(images, files),
        { imagesOnly: engine === "dsh" || engine === "codex" });
      if(draftStore.generation!==generation)return;
      if(currentKey.current!==targetKey && !sameDraftScope(currentKey.current,targetKey)){
        const prior=draftStore.get(targetKey);
        draftStore.set(targetKey,{...prior,images:[...prior.images,...batch.images],files:[...prior.files,...batch.files]});
        return;
      }
      if (batch.images.length) setImages((previous) => [...previous, ...batch.images]);
      if (batch.files.length && engine !== "dsh") {
        setFiles((previous) => [...previous, ...batch.files]);
      }
      const errors = [...batch.errors];
      if (errors.length) window.alert(errors.join("；"));
    } finally {
      attachmentImportLock.current = false;
      setImporting(false);
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    revision.current++;
    const fs = clipboardFiles(e.clipboardData);
    const pastedText = e.clipboardData.getData("text/plain");
    if (fs.length) {
      e.preventDefault(); setText(insertClipboardText(e.currentTarget, pastedText));
      void onPick(fs); return;
    }
    if (pastedText.length <= LONG_PASTE_THRESHOLD) return;
    e.preventDefault();
    setPastes((current) => [
      ...current,
      makeComposerPaste(pastedText, crypto.randomUUID()),
    ]);
  };

  const draft: ComposerDraft={input:text,images,files,pastes};
  const {reading:clipboardReading,pasteAndSend}=useClipboardSend({
    key:draftKey,label:cwd,store:draftStore,revision:()=>revision.current,
    isEmpty:()=>!draftHasContent({...draft,input:taRef.current?.value ?? text}),
    blocked:creating || importing || selectedDshPresetMissing || !!selectedDshPreset?.broken,
    onReady:copied=>{revision.current++;setText(copied.input);setImages(copied.images);setFiles(copied.files);send(copied.input, copied);},onNotice:setNotice,
  });
  const pasteMode=!text.trim() && !hasAttachments && !pastes.length;
  const canPaste=pasteMode && !creating && !importing && !clipboardReading
    && !selectedDshPresetMissing && !selectedDshPreset?.broken;
  const send = (value = taRef.current?.value ?? text, source: ComposerDraft = draft) => {
    const {images, files} = source;
    const hasAttachments = images.length > 0 || files.length > 0;
    const composed = composePastePrompt(source.pastes, value.trim());
    if (!composed.ok) {
      window.alert(`消息内容超过上限（最多 ${composed.maxChars.toLocaleString()} 个字符）`);
      return;
    }
    const prompt = composed.prompt;
    if ((!prompt && !hasAttachments) || creating || importing
        || selectedDshPresetMissing || !!selectedDshPreset?.broken) return;
    setCreating(true);
    const queued = onSend(
      prompt, images.length ? images : undefined, engine!=="dsh" && files.length ? files : undefined);
    if (!queued) setCreating(false);
    else draftStore.archive(draftKey,{...source,input:value},"sent",cwd);
  };

  const requestButtonSend = () => {
    if(!draftHasContent({...draft,input:taRef.current?.value ?? text})
      && !imeSubmitRef.current.shouldCommitBeforeButtonSubmit()){void pasteAndSend();return;}
    if (buttonSendTimerRef.current !== null) return;
    buttonSendTimerRef.current = window.setTimeout(() => {
      buttonSendTimerRef.current = null;
      send();
    }, 0);
  };
  const cwdButton = (
    <button className="newchat-cwd" onClick={onPickCwd}
      title="更改工作目录" disabled={creating}>
      <Icon name="folder" size={16} />
      <span className="newchat-cwd-path">
        {cwd === "~" ? "~ · 主目录" : (cwd || "未指定目录")}
      </span>
      <Icon name="edit" size={13} />
    </button>
  );
  const dshPresetSelector = engine === "dsh" ? (
    <label className="newchat-profile">
      <span>Agent</span>
      <select value={dshAgentPreset ?? ""}
        onChange={(event) => onPickDshPreset?.(event.target.value || null)}
        disabled={creating || importing || !onPickDshPreset}
        aria-label="选择 DSH Agent Preset">
        <option value="">
          {selectedDshPreset && dshAgentPreset === null
            ? `默认 · ${selectedDshPreset.name}` : "默认 Agent"}
        </option>
        {selectedDshPresetMissing && dshAgentPreset && (
          <option value={dshAgentPreset} disabled>已移除 Agent</option>
        )}
        {dshPresets.map((preset) => (
          <option key={preset.id} value={preset.id} disabled={!!preset.broken}>
            {preset.name}{preset.trust === "user" ? " · 用户" : ""}
            {preset.broken ? " · 不可用" : ""}
          </option>
        ))}
      </select>
    </label>
  ) : null;
  const dshPresetWarning = selectedDshPresetWarning ? (
    <div className="newchat-profile-error" role="status">
      <Icon name="warning" size={14} />
      <span>{selectedDshPresetWarning}</span>
    </div>
  ) : null;

  return (
    <div className={"newchat code-newchat" + (designed ? " designed-newchat" : "")}
      onPointerDownCapture={() => { metadataPressRef.current = null; }}
      onPointerCancelCapture={() => { metadataPressRef.current = null; }}>
      <div className="newchat-card">
        <div className="newchat-intro">
        <div className="newchat-greet">开始新对话</div>
        {onPickEngine && engines.some(item => item.id === "codex") ? (
          <div className="newchat-agent-picker" role="radiogroup" aria-label="选择 Agent">
            {(["dsh", "codex"] as const).map(id => {
              const item = engines.find(item => item.id === id);
              return <button type="button" key={id} role="radio" aria-checked={engine===id}
                disabled={!item?.available || creating} title={item?.reason ?? undefined}
                onClick={() => onPickEngine(id)}>{id==="dsh" ? "DSH" : "Codex"}</button>;
            })}
          </div>
        ) : <span className={`newchat-engine ${engine}`}>{engine === "codex" ? "Codex" : "DSH"}</span>}
        <div className="newchat-context">
          {dshPresetSelector}
          {cwdButton}
          {dshPresetWarning}
        </div>
        </div>

        {(hasAttachments || pastes.length > 0) && (
          <div className="attach show newchat-attach">
            <PendingImageAttachments images={images}
              onRemove={(index) => setImages((previous) =>
                previous.filter((_, candidate) => candidate !== index))} />
            {files.map((f, i) => (
              <span key={i} className="attach-file">
                <Icon name="read" size={14} />
                <span className="attach-fn">{f.filename}</span>
                <button className="attach-x" onClick={() => setFiles(files.filter((_, j) => j !== i))} aria-label="移除"><Icon name="close" size={12} /></button>
              </span>
            ))}
            <PasteCards pastes={pastes} onChange={setPastes}
              disabled={creating || importing} />
          </div>
        )}

        {notice && <p className="newchat-notice" role="status">{notice}</p>}
        <div className="newchat-editor">
        {designed && <button type="button" className="newchat-expand iconbtn"
          aria-label={expanded ? "收拢输入区" : "展开输入区"}
          onPointerDown={event => event.preventDefault()}
          onClick={() => {setExpanded(value => !value);taRef.current?.focus({preventScroll:true});}}>
          <Icon name={expanded ? "collapse" : "expand"} size={17}/>
        </button>}
        <textarea className="newchat-input"
          enterKeyHint={touchInput ? "enter" : undefined}
          placeholder="发条消息开始…" ref={taRef}
          value={text} onChange={(e) => {revision.current++;setText(e.target.value);}} onPaste={onPaste}
          autoFocus={autoFocus} rows={3}
          disabled={creating || importing}
          onCompositionStart={() => imeSubmitRef.current.startComposition()}
          onCompositionEnd={(e) => {
            imeSubmitRef.current.endComposition();
            revision.current++;
            setText(e.currentTarget.value);
          }}
          onKeyDown={(e) => {
            if (touchInput && !(e.metaKey || e.ctrlKey)) return;
            if (!imeSubmitRef.current.shouldSubmitKey({
              key: e.key, shiftKey: e.shiftKey,
              isComposing: e.nativeEvent.isComposing, keyCode: e.nativeEvent.keyCode,
            })) return;
            e.preventDefault();
            send(e.currentTarget.value);
          }} />

        <div className="newchat-foot">
          <div className="newchat-ctls">
            <DraftTools store={draftStore} draftKey={draftKey} label={cwd} draft={draft}
              disabled={creating || importing} reading={clipboardReading} onNotice={setNotice}
              onPhoto={()=>photoRef.current?.click()} onPasteSend={()=>{void pasteAndSend();}}
              onClear={()=>{revision.current++;setText("");setImages([]);setFiles([]);setPastes([]);}}
              onRestore={restored=>{revision.current++;setText(restored.input);setImages(restored.images);setFiles(restored.files);setPastes(restored.pastes);taRef.current?.focus({preventScroll:true});}} />
            <input ref={photoRef} type="file" accept="image/*" multiple
              aria-label="添加照片" hidden
              onChange={(e) => { void onPick(e.target.files); e.target.value = ""; }} />
            <input ref={fileRef} type="file" multiple aria-label="添加文件" hidden
              onChange={(e) => { void onPick(e.target.files); e.target.value = ""; }} />
            {!designed && <button type="button" className="hint-ctl"
              onPointerDown={beginMetadataPress}
              onClick={event => activateMetadata(event, () => setSheetKind("models"))}
              aria-label="选择模型" title="选择模型" disabled={creating || importing || !onPickModel}>
              {modelLabel}
            </button>}
            {!designed && <button type="button" className="hint-ctl"
              onPointerDown={beginMetadataPress}
              onClick={event => activateMetadata(event, () => setSheetKind("efforts"))}
              aria-label="选择思考强度" title="选择思考强度"
              disabled={creating || importing || !onPickEffort}>
              {effortLabel}
            </button>}
            {designed && <button type="button" className="composer-config-trigger" aria-label="会话配置"
              onPointerDown={beginMetadataPress}
              onClick={event => activateMetadata(event, () => setSettingsOpen(true))}
              disabled={creating || importing}>
              <Icon name="settings" size={18}/><span>{modelLabel}</span><Icon name="chev" size={12}/>
            </button>}
          </div>
          <div className="newchat-foot-right">
            {designed && onOpenSessions && <button type="button" className="newchat-sessions iconbtn"
              aria-label="打开会话" onPointerDown={event => event.preventDefault()} onClick={onOpenSessions}>
              <Icon name="message-square" size={19}/>
            </button>}
            <span className={"newchat-hint" + (createError || creating || importing ? " status" : "")}>{createError
              ? `创建失败：${createError}`
              : importing ? "正在导入附件…" : creating ? "正在创建会话…" : touchInput ? "换行继续输入" : "Enter 发送"}</span>
            <button className={"newchat-send" + (pasteMode ? " paste-mode" : "")} aria-label={pasteMode ? (clipboardReading ? "正在读取剪贴板" : "粘贴并发送") : "开始"}
              onPointerDown={() => {
                if (imeSubmitRef.current.shouldCommitBeforeButtonSubmit()) taRef.current?.blur();
              }}
              onClick={requestButtonSend}
              disabled={!canSend && !canPaste || clipboardReading}>
              {clipboardReading ? <span className="clipboard-reading"/> : <Icon name={pasteMode ? "paste-send" : "send"} size={16} />}<span className="newchat-send-label">{pasteMode ? "粘贴发送" : "开始"}</span>
            </button>
          </div>
        </div>
        </div>
      </div>

      <SessionSettingsSheet open={designed && settingsOpen} onClose={() => setSettingsOpen(false)}
        rows={[
          {label:"模型",value:modelLabel,icon:"cpu",actionLabel:"选择模型",disabled:creating || importing || !onPickModel,
            onClick:() => {setSettingsOpen(false);setSheetKind("models");}},
          {label:"思考强度",value:effortLabel,icon:"gauge3",actionLabel:"选择思考强度",disabled:creating || importing || !onPickEffort,
            onClick:() => {setSettingsOpen(false);setSheetKind("efforts");}},
        ]} note="未指定的设置沿用本机 DSH 默认值" />
      <NewChatSelectorSheet
        open={sheetKind !== null}
        kind={sheetKind ?? "models"}
        options={sheetKind === "efforts" ? effortOptions : modelOptions}
        current={sheetKind === "efforts" ? effort : model}
        onClose={() => setSheetKind(null)}
        onPick={(value) => {
          if (sheetKind === "efforts") onPickEffort?.(value);
          else onPickModel?.(value);
          setSheetKind(null);
        }}
      />
    </div>
  );
}
