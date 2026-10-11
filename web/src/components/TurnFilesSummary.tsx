import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../icons";

export function TurnFilesSummary({paths, onOpenFile, onOpenDiff}: {
  paths: string[];
  onOpenFile?: (path: string) => void;
  onOpenDiff?: () => void;
}) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", dismiss);
    return () => window.removeEventListener("keydown", dismiss);
  }, [open]);
  return <div className="turn-files">
    <button type="button" className="turn-files-summary" aria-haspopup="dialog" aria-expanded={open}
      onClick={() => setOpen(true)}><Icon name="edit" size={14} />改动 {paths.length} 个文件<Icon name="chevron-right" size={14} /></button>
    {open && createPortal(<>
      <div className="scrim show" onClick={() => setOpen(false)} />
      <section className="sheet show turn-files-sheet" role="dialog" aria-modal="true" aria-label="本轮改动文件">
        <div className="sheet-grip" />
        <header className="queued-query-head"><b>改动 {paths.length} 个文件</b>
          <button className="iconbtn" onClick={() => setOpen(false)} aria-label="关闭改动文件列表"><Icon name="close" /></button>
        </header>
        <div className="turn-files-sheet-list">
          {paths.map(path => <button key={path} disabled={!onOpenFile} onClick={() => {setOpen(false); onOpenFile?.(path);}}>
            <Icon name="read" size={17} /><span><b>{path.split("/").pop()}</b><small>{path}</small></span><Icon name="chevron-right" size={15} />
          </button>)}
        </div>
        {onOpenDiff && <button className="turn-files-view-diff" onClick={() => {setOpen(false); onOpenDiff();}}>查看全部差异<Icon name="chevron-right" size={14} /></button>}
      </section>
    </>, document.body)}
  </div>;
}
