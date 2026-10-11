import { useEffect, useRef, useState } from "react";

import { Icon } from "../icons";
import type { PendingQuery } from "../reducer";

import type { QueryImg, QueryFile } from "../protocol";
import { pickFiles, attachmentBytes } from "../img";
import { clipboardFiles, insertClipboardText } from "../clipboard-content";

import { MAX_PROMPT_CHARS } from "../composer-pastes";

export interface QueuedQueryEditor {
  actions?: boolean;
  editingHeld?: boolean;
  takeback?: boolean;
  sid: string;
  msgId: string;
  preview: string;
  prompt: string | null;
  kind: "queue" | "replace" | null;
  state: "submitting" | "queued" | "failed";
  imageCount: number;
  fileCount: number;
  images?: QueryImg[];
  files?: QueryFile[];
  loading: boolean;
  saving: boolean;
  error: string | null;
}

interface ChipProps {
  query: PendingQuery;
  onOpen: (query: PendingQuery) => void;
  onActions?: (query: PendingQuery) => void;
  onRemove: () => void;
  onTakeback: () => void;
}

export function QueuedQueryChip({
  query, onOpen, onActions, onRemove, onTakeback,
}: ChipProps) {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const start = useRef<{ x: number; y: number } | null>(null);
  const held = useRef(false);
  const cancel = () => { if (timer.current) clearTimeout(timer.current); timer.current = null; };
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  const fallback = (query.imageCount ?? query.images?.length ?? 0) > 0
    ? "图片" : "附件";
  const badge = query.queueState === "failed"
    ? "未发送"
    : query.queueState === "submitting"
      ? "提交中"
      : query.queueKind === "replace"
        ? "替换"
        : "待发送";
  return (
    <span className={`qchip${query.queueError ? " error" : ""}`}>
      <button type="button" className="qsummary"
        disabled={!query.msg_id}
        aria-label="编辑待发送消息"
        onPointerDown={event => {
          cancel(); held.current = false;
          if (event.button !== 0 || !onActions) return;
          start.current = { x: event.clientX, y: event.clientY };
          timer.current = setTimeout(() => { held.current = true; onActions(query); }, 500);
        }}
        onPointerMove={event => {
          if (start.current && Math.hypot(event.clientX-start.current.x,event.clientY-start.current.y)>10) cancel();
        }}
        onPointerUp={cancel} onPointerCancel={cancel} onPointerLeave={cancel}
        onContextMenu={event => { event.preventDefault(); cancel(); held.current = true; onActions?.(query); }}
        onClick={event => { if (held.current) { event.preventDefault(); held.current = false; return; } onOpen(query); }}>
        <span className="qbadge">{badge}</span>
        <span className="qt">{query.prompt || fallback}</span>
      </button>
      <button type="button" className="qx" onClick={onRemove}
        aria-label="移出队列">
        <Icon name="close" size={12} />
      </button>
      {(query.queueState === "queued" || query.queueState === "failed") &&
        <button type="button" className="qreturn" onClick={onTakeback}
          aria-label="取消排队并取回到输入框" title="取回到输入框">
          取回
        </button>}
    </span>
  );
}

interface DialogProps {
  editor: QueuedQueryEditor | null;
  onClose: () => void;
  onBeginEdit?: () => void;
  onSave: (prompt: string, images?: QueryImg[], files?: QueryFile[]) => boolean;
  canAttach?: boolean;
  onRetry: () => boolean;
  onStartNow?: (prompt: string) => boolean;
  canStartNow?: boolean;
  onRemove?: () => void;
  onTakeback?: (prompt: string, images?: QueryImg[], files?: QueryFile[]) => void;
}

export function QueuedQueryDialog({
  editor, onClose, onBeginEdit, onSave, onRetry, onStartNow, canStartNow, onRemove, onTakeback, canAttach,
}: DialogProps) {
  const [editing, setEditing] = useState(!editor?.actions && editor?.state !== "submitting");
  const [draft, setDraft] = useState(
    () => editor?.prompt ?? editor?.preview ?? "");
  const detailLoadedRef = useRef(editor?.prompt != null);
  const [copyStatus, setCopyStatus] = useState("");
  const [addedImages, setAddedImages] = useState<QueryImg[]>([]);
  const [addedFiles, setAddedFiles] = useState<QueryFile[]>([]);
  const [importing, setImporting] = useState(false);
  const [attachmentError, setAttachmentError] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const importLock = useRef(false);
  const open = editor !== null;
  const prompt = editor?.prompt ?? null;
  const saving = (editor?.saving ?? false) || importing;

  useEffect(() => {
    if (prompt === null || detailLoadedRef.current) return;
    detailLoadedRef.current = true;
    setDraft(prompt);
  }, [prompt]);

  useEffect(() => { setCopyStatus(""); }, [draft]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose, open, saving]);

  if (!editor) return null;
  const attachmentCount = editor.imageCount + editor.fileCount + addedImages.length + addedFiles.length;
  const emptyDraft = draft.trim().length === 0 && attachmentCount === 0;
  const canEdit = editor.state !== "submitting";
  const canRetry = editor.state === "failed"
    || (editor.state === "queued" && !!editor.error);
  const importAttachments = async (list: File[]) => {
    if (importLock.current || saving || !canAttach || !list.length) return;
    importLock.current = true; setImporting(true);
    try {
      const batch = await pickFiles(list, attachmentCount, attachmentBytes(addedImages, addedFiles));
      setAddedImages(items => [...items, ...batch.images]);
      setAddedFiles(items => [...items, ...batch.files]);
      setAttachmentError(batch.errors.join("；"));
    } finally { importLock.current = false; setImporting(false); }
  };

  return (
    <>
      <div className="scrim show" onClick={() => {
        if (!saving) onClose();
      }} />
      <section className="sheet show queued-query-sheet" role="dialog"
        aria-modal="true" aria-label="待发送消息">
        <div className="sheet-grip" />
        <div className="queued-query-head">
          <div>
            <div className="sheet-title">待发送消息</div>
            <div className="queued-query-kind">
              {editor.state === "failed"
                ? "提交失败，内容仍保存在当前浏览器"
                : editor.state === "submitting"
                  ? "正在等待服务端确认"
                  : editor.kind === "replace"
                    ? "下一条替换消息"
                    : editor.actions && !editing
                      ? "引导会先停止当前任务，再处理这条消息"
                      : editor.editingHeld ? "编辑期间暂停发送，保存后继续排队" : "当前任务结束后自动发送"}
            </div>
          </div>
          <div className="queued-query-head-actions">
          <button type="button" className="iconbtn"
            disabled={editor.loading || editor.prompt === null}
            aria-label="复制排队消息" title={copyStatus || "复制全文"}
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(editing ? draft : editor.prompt ?? "");
                setCopyStatus("已复制");
              } catch { setCopyStatus("复制失败，请手动选择文字复制"); }
            }}><Icon name={copyStatus === "已复制" ? "check" : "copy"} size={17} /></button>
          <button type="button" className="iconbtn" onClick={onClose}
            disabled={saving} aria-label="关闭排队消息详情">
            <Icon name="close" size={17} />
          </button>
          </div>
        </div>

        <div className="queued-query-body">
          {copyStatus && <div className="queued-query-copy-status" role="status">{copyStatus}</div>}
          {editor.loading ? (
            <div className="queued-query-loading" role="status">
              正在读取完整消息…
            </div>
          ) : editor.prompt === null ? (
            <div className="queued-query-error" role="alert">
              {editor.error ?? "无法读取这条排队消息。"}
            </div>
          ) : editing ? (
            <textarea className="queued-query-editor" value={draft}
              maxLength={MAX_PROMPT_CHARS}
              autoFocus
              aria-label="编辑排队消息"
              onPaste={event => {
                const files = clipboardFiles(event.clipboardData);
                if (!files.length) return;
                event.preventDefault();
                if (!canAttach) { setAttachmentError("附件编辑服务尚未就绪，请稍后重试"); return; }
                setDraft(insertClipboardText(event.currentTarget, event.clipboardData.getData("text/plain")));
                void importAttachments(files);
              }}
              onChange={(event) => setDraft(event.target.value)} />
          ) : (
            <div className="queued-query-prompt" data-testid="queued-full-prompt">
              {editor.prompt || "（仅包含附件）"}
            </div>
          )}

          {editing && canEdit && <div className="queued-attachment-editor">
            <input ref={fileInput} type="file" multiple hidden aria-label="添加排队消息附件"
              disabled={saving || !canAttach}
              onChange={event => {
                const list = Array.from(event.target.files ?? []); event.target.value = "";
                void importAttachments(list);
              }} />
            <button type="button" className="queued-add-attachment" disabled={saving || !canAttach || editor.loading}
              title={canAttach ? "添加图片或文件" : "附件编辑服务尚未就绪"}
              onClick={() => fileInput.current?.click()}><Icon name="plus" size={16} />{importing ? "正在读取…" : "添加附件"}</button>
            <div className="queued-new-attachments">
              {addedImages.map((image, index) => <button key={`image:${index}`} type="button" disabled={saving}
                aria-label={`移除新增图片 ${index + 1}`} onClick={() => setAddedImages(items => items.filter((_, i) => i !== index))}>
                <img src={`data:${image.media_type};base64,${image.data}`} alt="新增图片" /><Icon name="close" size={12} /></button>)}
              {addedFiles.map((file, index) => <button key={`file:${index}`} type="button" disabled={saving}
                aria-label={`移除新增附件 ${file.filename}`} onClick={() => setAddedFiles(items => items.filter((_, i) => i !== index))}>
                <Icon name="read" size={14} /><span>{file.filename}</span><Icon name="close" size={12} /></button>)}
            </div>
            {attachmentError && <p role="alert">{attachmentError}</p>}
          </div>}
          {!editor.loading && editor.prompt !== null && (editor.imageCount + editor.fileCount) > 0 && (
            <div className="queued-query-attachments">
              {editor.imageCount > 0 && <span>{editor.imageCount} 张图片</span>}
              {editor.fileCount > 0 && <span>{editor.fileCount} 个文件</span>}
              <span>编辑文字不会移除附件</span>
            </div>
          )}
          {!editor.loading && editor.error && editor.prompt !== null && (
            <div className="queued-query-error" role="alert">{editor.error}</div>
          )}
        </div>

          {!editor.loading && editor.prompt !== null && (canEdit || canRetry) && (
          <div className="queued-query-actions">
            {onRemove && <button type="button" className="queued-query-secondary"
              disabled={saving} onClick={onRemove}>移除</button>}
            {editor.takeback && onTakeback && editor.state === "queued" &&
              <button type="button" className="queued-query-secondary" disabled={saving || !!editor.error}
                onClick={() => onTakeback(draft,
                  [...(editor.images ?? []), ...addedImages],
                  [...(editor.files ?? []), ...addedFiles])}>
                取消排队并取回
              </button>}
            {editor.actions && !editing && canStartNow && editor.state === "queued" && onStartNow &&
              <button type="button" className="queued-query-secondary" disabled={saving || emptyDraft}
                onClick={() => onStartNow(editing ? draft : editor.prompt ?? "")}>引导当前任务</button>}
            {editing ? (
              <>
                <button type="button" className="queued-query-secondary"
                  disabled={saving}
                  onClick={() => {
                    onClose();
                  }}>
                  取消
                </button>
                <button type="button" className="queued-query-primary"
                  disabled={editor.saving || emptyDraft}
                  onClick={() => {
                    if (onSave(draft, addedImages, addedFiles) && editor.state === "failed") {
                      setEditing(false);
                    }
                  }}>
                  {editor.saving ? "正在保存…" : "保存修改"}
                </button>
              </>
            ) : (
              <>
                {canEdit && (
                  <button type="button" className="queued-query-secondary"
                    disabled={saving}
                    onClick={() => { detailLoadedRef.current = false; onBeginEdit?.(); setEditing(true); }}>
                    <Icon name="edit" size={14} />编辑
                  </button>
                )}
                {canRetry && (
                  <button type="button" className="queued-query-primary"
                    disabled={saving}
                    onClick={onRetry}>
                    {editor.saving ? "正在重试…" : "重试发送"}
                  </button>
                )}
              </>
            )}
          </div>
        )}
      </section>
    </>
  );
}
