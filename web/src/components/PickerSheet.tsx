import { useEffect, useRef, type ReactNode } from "react";

import { useAnimatedPresence } from "../use-animated-presence";

/** Closed pickers leave the DOM after a short inert fade. They are never
 * retained as off-screen translated panels that a keyboard can reveal. */
export function PickerSheet({ open, title, heading = title, className = "", onClose, children }: {
  open: boolean;
  title: string;
  heading?: string;
  className?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const present = useAnimatedPresence(open);
  const snapshot = useRef({title, heading, className, children});
  if (open) snapshot.current = {title, heading, className, children};
  const visible = snapshot.current;
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      onClose();
    };
    window.addEventListener("keydown", dismiss);
    return () => window.removeEventListener("keydown", dismiss);
  }, [open, onClose]);

  if (!present) return null;
  return <>
    <div className={`scrim show picker-scrim${open ? "" : " motion-exit"}`} onClick={open ? onClose : undefined} />
    <div className={`sheet show picker-sheet ${visible.className}${open ? "" : " motion-exit"}`} role="dialog" aria-hidden={!open} inert={!open} aria-modal="true" aria-label={visible.title}>
      <div className="sheet-grip" />
      <div className="sheet-title">{visible.heading}</div>
      <div className="sheet-scroll">{visible.children}</div>
    </div>
  </>;
}
