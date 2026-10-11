import { useEffect, useRef, useState } from "react";
import { draftHasContent, type ComposerDraft, type ComposerDraftStore } from "./composer-drafts";
import { ClipboardReadError, readClipboardDraft } from "./clipboard-content";

interface Options {
  key: string;
  label: string;
  store: ComposerDraftStore;
  revision: () => number;
  isEmpty: () => boolean;
  blocked: boolean;
  onReady: (draft: ComposerDraft) => void;
  onNotice: (message: string) => void;
}

/** Start readText inside the click itself: putting it after a timer loses the
 * Safari user gesture. Delayed permission replies cannot target another draft. */
export function useClipboardSend(options: Options) {
  const latest=useRef(options);latest.current=options;
  const mounted=useRef(true);
  const pending=useRef(false);
  const [reading,setReading]=useState(false);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  const pasteAndSend=async () => {
    const target=latest.current;
    if(pending.current || target.blocked)return;
    if(!target.isEmpty()) {target.onNotice("请先发送或清空当前草稿");return;}
    if(!navigator.clipboard?.readText && !navigator.clipboard?.read) {target.onNotice("当前浏览器无法直接读取剪贴板，请长按输入框粘贴");return;}
    const revision=target.revision(),generation=target.store.generation;
    pending.current=true;setReading(true);
    try {
      const copied=await readClipboardDraft();
      if(target.store.generation!==generation)return;
      if(!copied.input.trim() && !copied.images.length) {if(mounted.current)latest.current.onNotice("剪贴板没有可发送的文字或图片");return;}
      target.store.archive(target.key,copied,"clipboard",target.label);
      const current=latest.current;
      if(!mounted.current || current.key!==target.key || current.revision()!==revision
          || current.blocked || !current.isEmpty()) {
        if(!draftHasContent(target.store.get(target.key)))target.store.set(target.key,copied);
        if(mounted.current)current.onNotice("输入状态已变化，剪贴板内容已保存为草稿，未发送");
        return;
      }
      current.onReady(copied);
    } catch (error) {
      if(mounted.current)latest.current.onNotice(error instanceof ClipboardReadError ? error.message : "未能读取剪贴板，请长按输入框选择“粘贴”");
    } finally {
      pending.current=false;if(mounted.current)setReading(false);
    }
  };
  return {reading,pasteAndSend};
}
