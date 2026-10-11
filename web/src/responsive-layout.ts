export type SidebarSwipeAction = "open" | "close" | null;

const SWIPE_THRESHOLD_PX = 50;
const HORIZONTAL_INTENT_RATIO = 1.25;

export function resolveSidebarSwipe(
  startX: number,
  startY: number,
  endX: number,
  endY: number,
  viewportWidth: number,
  locked: boolean,
  open = false,
  side: "left" | "right" = "left",
): SidebarSwipeAction {
  if (locked) return null;
  const dx = endX - startX;
  const dy = endY - startY;
  if (Math.abs(dx) <= SWIPE_THRESHOLD_PX
      || Math.abs(dx) <= Math.abs(dy) * HORIZONTAL_INTENT_RATIO) return null;
  if (side === "right") {
    if (!open && dx < 0 && startX >= viewportWidth - 40) return "open";
    return open && dx > 0 ? "close" : null;
  }
  if (dx > 0 && startX < viewportWidth / 3) return "open";
  return dx < 0 ? "close" : null;
}

const PANEL_MIN_WIDTH_PX = 360;
const CHAT_MIN_WIDTH_PX = 420;
const PANEL_MAX_VIEWPORT_RATIO = 0.72;

export function clampPanelWidth(width: number, viewportWidth: number): number {
  const maxWidth = Math.max(
    PANEL_MIN_WIDTH_PX,
    Math.min(viewportWidth - CHAT_MIN_WIDTH_PX, viewportWidth * PANEL_MAX_VIEWPORT_RATIO),
  );
  return Math.round(Math.min(Math.max(width, PANEL_MIN_WIDTH_PX), maxWidth));
}

export interface DeviceLayout {
  kind: "phone" | "tablet" | "desktop";
  overlay: boolean;
  touchInput: boolean;
  sidebarSide: "left" | "right";
}
export function classifyDeviceLayout(
  width: number, touchInput: boolean, screenShortSide = width,
): DeviceLayout {
  // Screen geometry keeps phone landscape and tablet split-view on their own
  // sidebar side. User-agent strings change in Safari's desktop-site mode.
  const kind = touchInput ? screenShortSide >= 600 ? "tablet" : "phone"
    : width < 980 ? "phone" : "desktop";
  return { kind, overlay: kind !== "desktop", touchInput, sidebarSide: kind === "phone" ? "right" : "left" };
}

export function readDeviceLayout(): DeviceLayout {
  if (typeof window === "undefined") return classifyDeviceLayout(1280, false);
  const touch = (typeof navigator !== "undefined" && navigator.maxTouchPoints > 0)
    || window.matchMedia("(any-pointer: coarse)").matches;
  const shortSide = Math.min(window.screen.width, window.screen.height);
  return classifyDeviceLayout(window.innerWidth, touch, shortSide > 0 ? shortSide : window.innerWidth);
}

export function applyDeviceLayout(layout: DeviceLayout): void {
  const attributes = {
    "data-device-layout": layout.kind,
    "data-touch-layout": String(layout.overlay),
    "data-input-mode": layout.touchInput ? "touch" : "keyboard",
    "data-sidebar-side": layout.sidebarSide,
  };
  for (const [key, value] of Object.entries(attributes)) {
    if (document.documentElement.getAttribute(key) !== value) {
      document.documentElement.setAttribute(key, value);
    }
  }
}
