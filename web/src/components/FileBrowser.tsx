import { useEffect, useRef, useState } from "react";
import type { FilesList } from "../protocol";
import { Icon } from "../icons";

export function FileBrowser({cwd, sid, response, onBrowse, onOpenFile, onClose}: {
  cwd: string; sid: string; response: FilesList | null;
  onBrowse: (path: string, query: string) => string | null;
  onOpenFile: (path: string) => void; onClose: () => void;
}) {
  const panelRef = useRef<HTMLElement>(null);
  const [path, setPath] = useState(".");
  const [query, setQuery] = useState("");
  const [requestId, setRequestId] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const browseRef = useRef(onBrowse);
  browseRef.current = onBrowse;
  const current = response?.sid === sid && response.request_id === requestId ? response : null;
  const currentRef = useRef(current);
  currentRef.current = current;
  useEffect(() => {
    setRequestId(null); setProblem(null);
    let timeout: number | undefined;
    const debounce = window.setTimeout(() => {
      const id = browseRef.current(path, query.trim());
      setRequestId(id);
      if (!id) setProblem("连接不可用，请关闭后重试");
      else timeout = window.setTimeout(() => {
        if (!currentRef.current) setProblem("读取超时，请重试");
      }, 10000);
    }, query ? 250 : 0);
    return () => { window.clearTimeout(debounce); window.clearTimeout(timeout); };
  }, [path, query]);
  useEffect(() => {
    const origin = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = requestAnimationFrame(() => panelRef.current?.querySelector<HTMLButtonElement>("button")?.focus({preventScroll:true}));
    return () => { cancelAnimationFrame(frame); if(origin?.isConnected) origin.focus({preventScroll:true}); };
  }, []);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key !== "Tab") return;
      const fields = Array.from(panelRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]),input') ?? []);
      const first = fields[0], last = fields.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [onClose]);
  const browse = (next: string) => { setQuery(""); setPath(next); };
  return <div className="dp-overlay" data-lock-horizontal-swipe onClick={onClose}>
    <section ref={panelRef} className="dp file-browser" role="dialog" aria-modal="true" aria-label="项目文件"
      onClick={event => event.stopPropagation()}>
      <div className="dp-head"><span className="dp-title">项目文件</span>
        <button className="iconbtn" aria-label="关闭项目文件" onClick={onClose}><Icon name="close" /></button></div>
      <div className="dp-crumbs" title={cwd}>{cwd}{path !== "." ? `/${path}` : ""}</div>
      <div className="file-browser-search"><Icon name="search" size={18} />
        <input aria-label="搜索项目文件" placeholder="搜索文件名或路径…" value={query}
          onChange={event => setQuery(event.target.value)} /></div>
      <div className="dp-list">
        {path !== "." && <button className="dp-row up" onClick={() => browse(current?.parent || ".")}>
          <Icon name="back" size={16}/><span>返回上级</span></button>}
        {current?.error || problem ? <div className="dp-empty" role="alert">{current?.error || problem}</div>
          : !current ? <div className="dp-empty" role="status">正在读取…</div>
          : current.entries.length === 0 ? <div className="dp-empty">{query ? "没有匹配文件" : "目录为空"}</div>
          : current.entries.map(entry => <button key={entry.path} className="dp-row"
            title={entry.path} onClick={() => entry.kind === "directory" ? browse(entry.path) : onOpenFile(entry.path)}>
            <Icon name={entry.kind === "directory" ? "folder" : "read"} size={18} />
            <span className="file-browser-name"><b>{entry.name}</b>{query && <small>{entry.path}</small>}</span>
            <Icon name="chevron-right" size={14} />
          </button>)}
        {current?.truncated && <div className="dp-empty">仅显示部分结果，请缩小目录或细化关键词</div>}
      </div>
      <div className="file-browser-foot">{query ? "搜索当前目录及子目录的文件名与路径" : "点按文件查看内容"}</div>
    </section>
  </div>;
}
