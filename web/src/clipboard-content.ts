import { emptyDraft, type ComposerDraft } from "./composer-drafts.ts";
import { MAX_PROMPT_CHARS } from "./composer-pastes.ts";
import { pickFiles, MAX_ATTACHMENT_COUNT } from "./img.ts";

export class ClipboardReadError extends Error {}

/** Safari can expose files without DataTransfer.items. Use one representation
 * so the same screenshot is not attached twice. Never fetch HTML image URLs. */
export function clipboardFiles(data: DataTransfer): File[] {
  if (data.files?.length) return Array.from(data.files).slice(0, MAX_ATTACHMENT_COUNT + 1);
  const files: File[] = [];
  for (const item of Array.from(data.items ?? [])) {
    if (item.kind !== "file") continue;
    const file = item.getAsFile();
    if (file) files.push(file);
    if (files.length > MAX_ATTACHMENT_COUNT) break;
  }
  return files;
}

/** Called from a direct click, before any await, retaining Safari activation.
 * Prefer the image representation of each clipboard item, then plain text. */
export async function readClipboardDraft(): Promise<ComposerDraft> {
  const draft = emptyDraft();
  if (!navigator.clipboard) throw new ClipboardReadError("当前浏览器无法读取剪贴板，请长按输入框粘贴");
  if (navigator.clipboard.read) {
    const items = await navigator.clipboard.read();
    if (items.length > MAX_ATTACHMENT_COUNT) throw new ClipboardReadError("剪贴板内容过多，请分批粘贴");
    const files: File[] = [];
    for (const item of items.slice(0, MAX_ATTACHMENT_COUNT + 1)) {
      const type = ["image/png", "image/jpeg", "image/webp"].find(type => item.types.includes(type));
      if (type) files.push(new File([await item.getType(type)], `粘贴图片-${files.length + 1}.${type.split("/")[1]}`, {type}));
      else if (item.types.some(type => type.startsWith("image/"))) throw new ClipboardReadError("剪贴板图片格式不支持，请粘贴 PNG、JPEG 或 WebP");
      if (item.types.includes("text/plain")) {
        const text = await (await item.getType("text/plain")).text();
        if (draft.input.length + text.length > MAX_PROMPT_CHARS) throw new ClipboardReadError("剪贴板文字超过消息上限，请分段粘贴");
        draft.input += text;
      }
    }
    const batch = await pickFiles(files, 0, 0, {imagesOnly:true});
    if (batch.errors.length) throw new ClipboardReadError(batch.errors.join("；"));
    draft.images = batch.images;
  } else {
    draft.input = await navigator.clipboard.readText();
  }
  if (draft.input.length > MAX_PROMPT_CHARS) throw new ClipboardReadError("剪贴板文字超过消息上限，请分段粘贴");
  return draft;
}

/** Preserve accompanying text at the caret when an image paste is intercepted. */
export function insertClipboardText(control: HTMLTextAreaElement, text: string): string {
  if (text) control.setRangeText(text, control.selectionStart, control.selectionEnd, "end");
  return control.value;
}
