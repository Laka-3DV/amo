import { useLayoutEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../icons";
import { useImeSubmit } from "../use-ime-submit";

export function RenameSessionDialog({title, onSave, onClose}: {
  title: string; onSave: (title: string) => void; onClose: () => void;
}) {
  const [value, setValue] = useState(title);
  const ime = useImeSubmit<HTMLInputElement>(text => { if (text.trim() && text.trim().length <= 200) onSave(text.trim()); });
  useLayoutEffect(() => {
    ime.inputRef.current?.focus({preventScroll:true});
    ime.inputRef.current?.select();
  }, [ime.inputRef]);
  return createPortal(<>
    <div className="scrim show rename-session-scrim" onClick={onClose} />
    <section className="sheet show rename-session-sheet" role="dialog" aria-modal="true" aria-label="重命名会话"
      onKeyDown={event => {
        if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
        if (event.key === "Escape") { event.preventDefault(); onClose(); }
        if (event.key !== "Tab") return;
        const controls = event.currentTarget.querySelectorAll<HTMLInputElement | HTMLButtonElement>("input,button:not(:disabled)");
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }}>
      <header className="queued-query-head"><b className="sheet-title">重命名会话</b>
        <button type="button" className="iconbtn" aria-label="关闭重命名" onClick={onClose}><Icon name="close" /></button>
      </header>
      <div className="rename-session-body">
        <input ref={ime.inputRef} aria-label="会话标题" value={value} maxLength={200}
          onChange={event => setValue(event.target.value)}
          onCompositionStart={ime.startComposition}
          onCompositionEnd={event => { ime.endComposition(); setValue(event.currentTarget.value); }}
          onKeyDown={event => {
            if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
            if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
            if (ime.shouldSubmitKey({key:event.key, shiftKey:event.shiftKey, isComposing:event.nativeEvent.isComposing, keyCode:event.nativeEvent.keyCode})) {
              event.preventDefault(); ime.requestSubmit();
            }
          }} />
      </div>
      <div className="queued-query-actions">
        <button type="button" className="queued-query-secondary" onClick={onClose}>取消</button>
        <button type="button" className="queued-query-primary" disabled={!value.trim() || value.trim().length > 200}
          onPointerDown={ime.commitCompositionBeforePointerSubmit} onClick={ime.requestSubmit}>保存</button>
      </div>
    </section>
  </>, document.body);
}
