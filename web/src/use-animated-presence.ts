import { useEffect, useState } from "react";

/** Short, inert exit before removal. Closed initial renders have no DOM, and
 * reduced-motion users get immediate removal on the next event-loop tick. */
export function useAnimatedPresence(open: boolean) {
  const [present, setPresent] = useState(open);
  useEffect(() => {
    if (open) { setPresent(true); return; }
    if (!present) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const timer = window.setTimeout(() => setPresent(false), reduced ? 0 : 160);
    return () => window.clearTimeout(timer);
  }, [open, present]);
  return present || open;
}
