import { createRoot } from "react-dom/client";
import "./index.css";
import "./App.css";
import "./appearance.css";
import "./interface-layouts.css";
import "./motion.css";
import { readAppearance, appearanceLayout } from "./appearance";
import {applyDeviceLayout,readDeviceLayout} from "./responsive-layout";
import App from "./App";
import { ErrorBoundary } from "./ErrorBoundary";
import { useMobileViewport } from "./use-mobile-viewport";
import { useEffect } from "react";
import { startViewportProbe } from "./viewport-probe";

export function RootApp() {
  useMobileViewport();
  useEffect(startViewportProbe, []);
  return <App />;
}

applyDeviceLayout(readDeviceLayout());
document.documentElement.setAttribute("data-engine", "dsh");
try {
  const savedSpacing = localStorage.getItem("cc_remote_chat_spacing");
  document.documentElement.setAttribute("data-chat-spacing", savedSpacing === "standard" || savedSpacing === "relaxed" ? savedSpacing : "compact");
  const appearance = readAppearance(localStorage);
  document.documentElement.setAttribute("data-appearance", appearance);
  document.documentElement.setAttribute("data-layout", appearanceLayout(appearance));
  document.documentElement.setAttribute("data-theme", localStorage.getItem("cc_remote_theme") === "dark" ? "dark" : "light");
} catch { document.documentElement.setAttribute("data-appearance", "blue"); }

createRoot(document.getElementById("root")!).render(
  <ErrorBoundary>
    <RootApp />
  </ErrorBoundary>
);

if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => {
    void navigator.serviceWorker.register("/sw.js");
  });
}
