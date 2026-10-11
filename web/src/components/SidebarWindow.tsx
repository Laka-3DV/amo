import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";

export interface SidebarWindowRow {
  key: string;
  kind: "card" | "head" | "more";
  indented?: boolean;
  render: () => ReactNode;
}

const BATCH_SIZE = 40;

/** Mount the newest batch first; native scrolling owns the viewport. Older
 * rows are prepended only near the top, with one exact anchor correction. */
export function SidebarWindow({ rows, scrollRef, visit, open }: {
  rows: SidebarWindowRow[];
  scrollRef: RefObject<HTMLDivElement | null>;
  visit: string;
  open: boolean;
}) {
  const [range, setRange] = useState({visit, firstKey: rows[Math.max(0, rows.length - BATCH_SIZE)]?.key});
  const currentRange = range.visit === visit ? range
    : {visit, firstKey: rows[Math.max(0, rows.length - BATCH_SIZE)]?.key};
  if (range.visit !== visit) setRange(currentRange);
  const firstIndex = rows.findIndex(row => row.key === currentRange.firstKey);
  const start = firstIndex < 0 ? Math.max(0, rows.length - BATCH_SIZE) : firstIndex;
  // Retain the first visible directory label without mounting every older
  // sibling in a large expanded project.
  let heading = start;
  if (rows[start]?.indented) {
    while (heading > 0 && rows[heading]?.kind !== "head") heading--;
  }
  const mounted = heading < start && rows[heading]?.kind === "head"
    ? [rows[heading], ...rows.slice(start)] : rows.slice(start);
  const windowRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLButtonElement>(null);
  const previousVisit = useRef("");
  const previousKeys = useRef("");
  const pendingAnchor = useRef<{element: HTMLElement; top: number} | null>(null);
  const mountedKeys = mounted.map(row => row.key).join("\u0000");
  const captureAnchor = useCallback(() => {
    const scroller = scrollRef.current;
    if (!scroller) return null;
    const bounds = scroller.getBoundingClientRect();
    for (const element of windowRef.current?.querySelectorAll<HTMLElement>(".sidebar-window-row") ?? []) {
      const box = element.getBoundingClientRect();
      if (box.bottom > bounds.top && box.top < bounds.bottom) return {element, top: box.top};
    }
    return null;
  }, [scrollRef]);
  // Structural changes only. Token/status refreshes never measure every row.
  const anchor = pendingAnchor.current ?? (open && previousVisit.current === visit
    && previousKeys.current !== mountedKeys ? captureAnchor() : null);
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (open && el) {
      if (previousVisit.current !== visit) el.scrollTop = el.scrollHeight;
      else if (anchor?.element.isConnected) {
        const delta = anchor.element.getBoundingClientRect().top - anchor.top;
        if (Math.abs(delta) > .5) el.scrollTop += delta;
      }
    }
    pendingAnchor.current = null;
    previousVisit.current = visit;
    previousKeys.current = mountedKeys;
  });
  const loadOlder = useCallback(() => {
    if (!open || start === 0) return;
    pendingAnchor.current = captureAnchor();
    setRange({visit, firstKey: rows[Math.max(0, start - BATCH_SIZE)].key});
  }, [open, start, visit, rows, captureAnchor]);
  useEffect(() => {
    const sentinel = sentinelRef.current;
    const root = scrollRef.current;
    if (!open || !sentinel || !root || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) loadOlder();
    }, {root, rootMargin: "500px 0px 0px 0px"});
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [open, scrollRef, loadOlder]);
  return <div className="sidebar-window" ref={windowRef}
    data-windowed-count={rows.length} data-mounted-count={mounted.length}>
    {start > 0 && <button ref={sentinelRef} type="button" className="sidebar-load-older"
      onClick={loadOlder}>较早的会话 · {start}</button>}
    {mounted.map(row => <div key={row.key}
      className={`sidebar-window-row ${row.kind}${row.indented ? " indented" : ""}`}>
      {row.render()}
    </div>)}
  </div>;
}
