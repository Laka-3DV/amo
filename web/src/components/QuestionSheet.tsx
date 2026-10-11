import { useState } from "react";
import type { AskOption } from "../protocol";
import { Icon } from "../icons";
import { useImeSubmit } from "../use-ime-submit";

interface Props {
  question: string;
  header?: string | null;
  options: AskOption[];
  allowText?: boolean;
  secret?: boolean;
  multiSelect?: boolean;
  onAnswer: (answer: string | string[]) => void;
  /** Dismiss the prompt without choosing; DSH then decides what an unanswered
   *  request means (fail-closed for approvals). */
  onSkip?: () => void;
  onCancel?: () => void;
  cancelling?: boolean;
  /** An answer is in flight: keep the card (and its options) until the wrapper
   *  confirms the prompt closed, so a lost frame is visible and retryable
   *  instead of looking like a tap that did nothing. */
  busy?: boolean;
}

export function QuestionSheet({
  question, header, options, allowText, secret, multiSelect, onAnswer, onSkip,
  onCancel, cancelling = false,
  busy = false,
}: Props) {
  const [text, setText] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const imeSubmit = useImeSubmit<HTMLInputElement>((value) => {
    if (busy || cancelling || !value.trim()) return;
    onAnswer(multiSelect ? [...selected, value] : value);
  });
  const toggle = (label: string) => {
    setSelected((current) => current.includes(label)
      ? current.filter((value) => value !== label)
      : [...current, label]);
  };
  return (
    <>
      <div className="scrim show" />
      <div className="sheet show question-sheet" role="dialog" aria-modal="true" aria-label="操作确认">
        <div className="sheet-grip" />
        <div className="sheet-title">
          <span className="qa-ic"><Icon name="spark" size={15} /></span>
          {header || "助手想确认一下"}
          {onCancel && <button type="button" className="qa-close iconbtn"
            aria-label="取消回答并停止任务" disabled={cancelling}
            onClick={onCancel}><Icon name="close" size={18} /></button>}
        </div>
        <div className="sheet-scroll">
          <div className="qa-question">{question}</div>
          <div className="qa-options">
            {options.map((o, i) => (
              <button key={i}
                className={`qa-opt${selected.includes(o.label) ? " selected" : ""}`}
                aria-pressed={multiSelect ? selected.includes(o.label) : undefined}
                disabled={busy || cancelling}
                onClick={() => multiSelect ? toggle(o.label) : onAnswer(o.label)}>
                <span className="qa-opt-label">{o.label}</span>
                {o.ds && <span className="qa-opt-ds">{o.ds}</span>}
              </button>
            ))}
            {multiSelect && (
              <button className="qa-multi-submit"
                disabled={busy || cancelling || selected.length === 0}
                onClick={() => onAnswer(selected)}>确定</button>
            )}
          </div>
          {allowText && <div className="qa-text-answer">
            <input ref={imeSubmit.inputRef} type={secret ? "password" : "text"} value={text}
              disabled={busy || cancelling}
              autoFocus={options.length === 0} placeholder={secret ? "输入敏感内容" : "输入回答"}
              onChange={(e) => setText(e.target.value)}
              onCompositionStart={imeSubmit.startComposition}
              onCompositionEnd={(e) => {
                imeSubmit.endComposition();
                setText(e.currentTarget.value);
              }}
              onKeyDown={(e) => {
                if (!imeSubmit.shouldSubmitKey({
                  key: e.key,
                  shiftKey: e.shiftKey,
                  isComposing: e.nativeEvent.isComposing,
                  keyCode: e.nativeEvent.keyCode,
                })) return;
                e.preventDefault();
                imeSubmit.requestSubmit();
              }} />
            <button disabled={busy || cancelling || !text.trim()}
              onPointerDown={imeSubmit.commitCompositionBeforePointerSubmit}
              onClick={imeSubmit.requestSubmit}>确定</button>
          </div>}
          {busy && !cancelling && (
            <div className="qa-pending" role="status" aria-live="polite">
              <span className="sp" aria-hidden="true" />已选择，正在发送…
            </div>
          )}
          {onSkip && !onCancel && (
            <button type="button" className="qa-skip" disabled={busy}
              onClick={onSkip}>
              跳过 · 交给 DSH 处理
            </button>
          )}
        </div>
        {onCancel && <div className="qa-footer">
          <button type="button" className="qa-cancel" disabled={cancelling}
            onClick={onCancel}>
            <Icon name="stop" size={16} />
            {cancelling ? "正在停止…" : "取消并停止任务"}
          </button>
        </div>}
      </div>
    </>
  );
}
