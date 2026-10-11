import { useEffect, useRef, useState, type TouchEvent } from "react";
import { resolveSidebarSwipe, type DeviceLayout } from "./responsive-layout";

type Side = "left" | "right";
interface Gesture {
  x: number; y: number; time: number; side: Side; open: boolean;
  width: number; dragging: boolean; locked: boolean; progress: number;
}

/** One React update chooses the edge; finger movement only writes a CSS
 * progress variable at most once per animation frame. At rest transform:none
 * keeps Safari's native scrolling and hit testing on the same coordinates. */
export function useSidebarGesture(layout: DeviceLayout, open: boolean, setOpen: (value: boolean) => void) {
  const shellRef = useRef<HTMLDivElement>(null);
  const [side, setSide] = useState<Side>(layout.sidebarSide);
  const gesture = useRef<Gesture | null>(null);
  const frame = useRef<number | null>(null);
  const suppressClick = useRef(false);
  const surfaces = useRef<{ panel: HTMLElement; scrim: HTMLElement } | null>(null);
  const getSurfaces = () => {
    if (surfaces.current && !shellRef.current?.contains(surfaces.current.panel)) surfaces.current = null;
    if (!surfaces.current && shellRef.current) {
      const panel = shellRef.current.querySelector<HTMLElement>(".sessions");
      const scrim = shellRef.current.querySelector<HTMLElement>(".scrim-side");
      if (panel && scrim) surfaces.current = { panel, scrim };
    }
    return surfaces.current;
  };
  const setProgress = (progress: number) => {
    const value = String(progress), targets = getSurfaces();
    shellRef.current?.style.setProperty("--drawer-progress", value);
    targets?.panel.style.setProperty("--drawer-progress", value);
    targets?.scrim.style.setProperty("--drawer-progress", value);
  };

  const clearFrame = () => {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
  };
  const clearDrag = () => {
    shellRef.current?.removeAttribute("data-drawer-dragging");
    shellRef.current?.style.removeProperty("--drawer-progress");
    const targets = getSurfaces();
    targets?.panel.style.removeProperty("--drawer-progress");
    targets?.scrim.style.removeProperty("--drawer-progress");
  };
  const release = (nextOpen: boolean) => {
    clearFrame();
    setOpen(nextOpen);
    // Commit the new show class while keeping the finger's last position,
    // then let CSS settle from that position to the chosen endpoint.
    frame.current = requestAnimationFrame(() => {
      frame.current = null;
      clearDrag();
    });
    gesture.current = null;
  };

  useEffect(() => {
    setSide(layout.sidebarSide);
    gesture.current = null;
    clearFrame(); clearDrag();
    // Rotation does not change phone/tablet identity; crossing device modes
    // restores that mode's default edge without carrying a desktop offset.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layout.kind, layout.sidebarSide]);
  useEffect(() => () => {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
  }, []);

  const onTouchStart = (event: TouchEvent) => {
    suppressClick.current = false;
    clearFrame(); clearDrag();
    const touch = event.touches.length === 1 ? event.touches[0] : null;
    if (!touch || !layout.overlay) { gesture.current = null; return; }
    const target = event.target instanceof Element ? event.target : null;
    const edge: Side | null = touch.clientX <= 40 ? "left"
      : touch.clientX >= window.innerWidth - 40 ? "right" : null;
    const allowedEdge = edge !== null && (layout.kind === "tablet" || edge === layout.sidebarSide);
    const locked = !!target?.closest("textarea,input,select,[contenteditable],.card-menu,.sheet,.scrim:not(.scrim-side),.header-menu-card,.header-menu-scrim,.device-sheet")
      || !!target?.closest("[data-lock-horizontal-swipe]") && !target.closest(".sessions")
      || !open && !allowedEdge
      || !!target?.closest(".composer") && !allowedEdge;
    gesture.current = { x: touch.clientX, y: touch.clientY, time: performance.now(),
      side: open ? side : allowedEdge ? edge! : side, open, width: 0,
      dragging: false, locked, progress: open ? 1 : 0 };
  };

  const onTouchMove = (event: TouchEvent) => {
    const current = gesture.current;
    const touch = event.touches.length === 1 ? event.touches[0] : null;
    if (!current || current.locked) return;
    if (!touch) { if (current.dragging) release(current.open); else gesture.current=null; return; }
    const dx = touch.clientX-current.x, dy = touch.clientY-current.y;
    if (!current.dragging) {
      if (Math.abs(dy)>12 && Math.abs(dy)>=Math.abs(dx)/1.25) {
        current.locked = true; return;
      }
      if (Math.abs(dx)<12 || Math.abs(dx)<Math.abs(dy)*1.25) return;
      const direction = current.side === "left" ? 1 : -1;
      if (!current.open && dx*direction<=0 || current.open && dx*direction>=0) return;
      current.width = shellRef.current?.querySelector(".sessions")?.getBoundingClientRect().width ?? 352;
      current.dragging = true;
      suppressClick.current = true;
      setSide(current.side);
      shellRef.current?.setAttribute("data-drawer-dragging", "true");
    }
    const distance = dx*(current.side === "left" ? 1 : -1);
    current.progress = Math.min(1, Math.max(0, (current.open ? 1 : 0)+distance/current.width));
    if (frame.current === null) frame.current = requestAnimationFrame(() => {
      frame.current = null;
      if (gesture.current?.dragging) setProgress(gesture.current.progress);
    });
  };

  const onTouchEnd = (event: TouchEvent) => {
    const current = gesture.current, touch = event.changedTouches[0];
    if (!current || !touch || current.locked || event.touches.length>0) {
      if (current?.dragging) release(current.open); else { gesture.current = null; clearFrame(); clearDrag(); }
      return;
    }
    if (current.dragging) {
      clearFrame();
      setProgress(current.progress);
      const distance = (touch.clientX-current.x)*(current.side === "left" ? 1 : -1);
      const velocity = distance/Math.max(16, performance.now()-current.time);
      const commit = Math.abs(distance)>=50 || Math.abs(velocity)>.45 && Math.abs(distance)>=24;
      release(commit ? !current.open : current.open);
      return;
    }
    const action = resolveSidebarSwipe(current.x,current.y,touch.clientX,touch.clientY,
      window.innerWidth,current.locked,current.open,current.side);
    if (action === "open") {
      setSide(current.side);
      if (current.side !== side) {
        shellRef.current?.setAttribute("data-drawer-dragging", "true");
        setProgress(0);
        frame.current = requestAnimationFrame(() => { frame.current = null; release(true); });
      } else setOpen(true);
    }
    else if (action === "close" && current.open) setOpen(false);
    if (action) suppressClick.current = true;
    gesture.current = null;
  };
  const onTouchCancel = () => {
    const current = gesture.current;
    if (current?.dragging) release(current.open);
    else gesture.current = null;
  };
  return { shellRef, side, onTouchStart, onTouchMove, onTouchEnd, onTouchCancel,
    resetClick: () => { suppressClick.current = false; },
    suppressClick,
  };
}
