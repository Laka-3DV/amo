import {useEffect,useLayoutEffect,useMemo,useRef,useState} from "react";
import {createPortal} from "react-dom";
import {Icon} from "../icons";
import {PickerSheet} from "./PickerSheet";
import {draftHasContent,type ComposerDraft,type ComposerDraftStore,type DraftHistoryEntry,type DraftReason} from "../composer-drafts";

const REASONS: Record<DraftReason,string>={auto:"自动保存",saved:"手动保存",sent:"已提交",cleared:"清空前",replaced:"替换前",clipboard:"剪贴板"};
const date=new Intl.DateTimeFormat("zh-CN",{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
interface Props {
  store: ComposerDraftStore;
  draftKey: string;
  label: string;
  draft: ComposerDraft;
  busy?: boolean;
  disabled?: boolean;
  readOnly?: boolean;
  reading: boolean;
  onNotice?: (message: string) => void;
  onPhoto: () => void;
  onPasteSend: () => void;
  onClear: () => void;
  onRestore: (draft: ComposerDraft) => void;
}
export function DraftTools(p: Props) {
  const trigger=useRef<HTMLButtonElement>(null);
  const [pane,setPane]=useState<Element|null>(null);
  useLayoutEffect(()=>{setPane(trigger.current?.closest(".pane") ?? null);},[]);
  const [view,setView]=useState<"tools"|"history"|"entry"|"clear"|"replace"|"delete"|null>(null);
  const [selected,setSelected]=useState<DraftHistoryEntry|null>(null);
  const [all,setAll]=useState(true);
  const [revision,refresh]=useState(0);
  const [error,setError]=useState("");
  const warningSeen=useRef("");
  const warning=p.store.storageWarning;
  const onNotice=p.onNotice;
  useEffect(()=>{if(warning && warning!==warningSeen.current)onNotice?.(warning);warningSeen.current=warning;},[warning,onNotice]);
  useEffect(()=>{setError("");},[view]);
  useLayoutEffect(()=>{setView(null);setSelected(null);},[p.draftKey]);
  useEffect(()=>p.store.subscribe(()=>refresh(value=>value+1)),[p.store]);
  const hasDraft=draftHasContent(p.draft);
  const entries=useMemo(()=>{void revision;return view==="history" ? p.store.listHistory(p.draftKey,all) : [];},[view,p.store,p.draftKey,all,revision]);
  const selectedText=useMemo(()=>selected ? [...selected.draft.pastes.map(item=>item.text),selected.draft.input].filter(Boolean).join("\n\n") : "",[selected]);
  const restore=()=>{
    if(!selected || p.disabled)return;
    if(hasDraft && !p.store.archive(p.draftKey,p.draft,"replaced",p.label)){setError("当前内容过大，无法留档；请先复制或分段保存");return;}
    p.onRestore(selected.draft);setView(null);
  };
  const history=()=>{p.store.archive(p.draftKey,p.draft,"auto",p.label);setView("history");};
  const title=view==="history" ? "草稿历史" : view==="entry" ? "草稿详情" : view==="clear" ? "清空草稿？"
    : view==="replace" ? "替换当前草稿？" : view==="delete" ? "删除这份草稿记录？" : "输入工具";
  return <>
    <button ref={trigger} type="button" className={"cmdbtn draft-tools-trigger" + (warning ? " cache-warning" : "")} aria-label="输入工具" title={warning || "附件、剪贴板与草稿"}
      onPointerDown={event=>event.preventDefault()} onClick={()=>setView("tools")}><Icon name="plus" size={19}/></button>
    {pane && createPortal(<PickerSheet open={view!==null} title={title} onClose={()=>setView(null)} className="draft-tools-sheet">
      {view==="tools" && <div className="draft-tool-actions">
        <button type="button" disabled={p.disabled || p.readOnly} onClick={()=>{setView(null);p.onPhoto();}}><Icon name="image" size={18}/><span>添加照片</span></button>
        <button type="button" aria-label={p.busy ? "粘贴并排队" : "粘贴并发送"} disabled={p.disabled || p.readOnly || p.reading || hasDraft} onClick={()=>{void p.onPasteSend();setView(null);}}><Icon name="paste-send" size={19}/><span>{p.busy ? "粘贴并排队" : "粘贴并发送"}</span><small>无需发送确认</small></button>
        <button type="button" disabled={!hasDraft} onClick={()=>{if(!p.store.archive(p.draftKey,p.draft,"saved",p.label)){setError("内容过大，无法留档；请分段保存");return;}setView("history");}}><Icon name="read" size={18}/><span>保存当前草稿</span></button>
        <button type="button" onClick={history}><Icon name="history" size={18}/><span>草稿历史</span></button>
        <button type="button" className="danger" disabled={!hasDraft || p.disabled} onClick={()=>setView("clear")}><Icon name="trash" size={18}/><span>清空草稿</span></button>
      </div>}
      {view==="history" && <>
        <div className="draft-history-filter" role="group" aria-label="草稿范围">
          <button type="button" aria-pressed={all} onClick={()=>setAll(true)}>最近草稿</button>
          <button type="button" aria-pressed={!all} onClick={()=>setAll(false)}>当前会话</button>
        </div>
        <div className="draft-history-list">{entries.length ? entries.map(entry=><button type="button" key={entry.id} className="draft-history-item"
          onClick={()=>{setSelected(entry);setView("entry");}}>
          <span><b>{entry.label}</b><time>{date.format(entry.at)}</time></span>
          <p>{(entry.draft.input || entry.draft.pastes.find(item=>item.text)?.text || "").slice(0,160) || "图片或附件草稿"}</p>
          <small>{REASONS[entry.reason]}{entry.draft.images.length ? ` · ${entry.draft.images.length} 张图片` : ""}{entry.draft.files.length ? ` · ${entry.draft.files.length} 个附件` : ""}</small>
        </button>) : <p className="draft-history-empty">暂无草稿记录。输入暂停后会自动保存，也可以手动保存。</p>}</div>
      </>}
      {view==="entry" && selected && <div className="draft-history-detail">
        <button type="button" className="draft-history-back" onClick={()=>setView("history")}><Icon name="back" size={16}/>返回历史</button>
        <p>{selected.label} · {date.format(selected.at)} · {REASONS[selected.reason]}</p>
        <textarea aria-label="草稿内容" readOnly value={selectedText}/>
        <div className="draft-confirm-actions">
          <button type="button" className="danger" onClick={()=>setView("delete")}>删除记录</button>
          <button type="button" className="draft-primary" disabled={p.disabled} onClick={()=>hasDraft ? setView("replace") : restore()}>恢复到输入框</button>
        </div>
      </div>}
      {(view==="clear" || view==="replace" || view==="delete") && <div className="draft-confirm">
        <p>{view==="clear" ? "清空输入框、粘贴块与附件。清空前的内容会留在草稿历史中。"
          : view==="replace" ? "当前内容会先保存到草稿历史，再恢复所选草稿。恢复后不会自动发送。"
          : "这条历史记录将被删除，当前输入框内容不受影响。"}</p>
        <div className="draft-confirm-actions">
          <button type="button" onClick={()=>setView(view==="clear" ? "tools" : "entry")}>取消</button>
          <button type="button" className={view==="replace" ? "draft-primary" : "danger"} disabled={p.disabled}
            onClick={()=>{
              if(view==="clear"){if(!p.store.archive(p.draftKey,p.draft,"cleared",p.label)){setError("内容过大，无法留档；请先复制或分段保存，草稿尚未清空");return;}p.onClear();setView(null);}
              else if(view==="replace")restore();
              else if(selected){p.store.removeHistory(selected.id);setSelected(null);setView("history");}
            }}>{view==="clear" ? "确认清空" : view==="replace" ? "确认恢复" : "确认删除"}</button>
        </div>
      </div>}
      {error && <p className="draft-cache-error" role="alert">{error}</p>}
      <p className="draft-cache-note" role="status">{p.store.storageWarning || "保存在当前浏览器 · 最多 50 份记录 · 最长 30 天 · 按容量清理 · 退出登录会清除"}</p>
    </PickerSheet>,pane)}
  </>;
}
