// A one-time iOS keyboard investigation (or explicit diagnostic URL). No text,
// session ids, URLs or credentials leave the page. Each run lasts three minutes.
export function startViewportProbe(): () => void {
  const setting = new URLSearchParams(location.search).get("viewport_probe");
  const marker = "dsh-viewport-layout-v2";
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  let measured = false;
  try { measured = sessionStorage.getItem(marker) === "done"; } catch { /* private storage */ }
  if (setting === "0" || (setting !== "1" && (!ios || measured))) return () => {};
  const probe = crypto.randomUUID();
  const expiresAt = Date.now() + 180_000;
  let timer: number | undefined;
  let lastSent = 0;
  const sample = () => {
    timer = undefined;
    if (Date.now() > expiresAt || document.visibilityState !== "visible"
        || Date.now() - lastSent < 1100) return;
    const composer = document.querySelector(".composer");
    if (!composer) return;
    const viewport = window.visualViewport;
    const shell = document.querySelector(".shell")!.getBoundingClientRect();
    const body = document.body.getBoundingClientRect();
    const input = composer.getBoundingClientRect();
    const hint = composer.querySelector(".hint")?.getBoundingClientRect();
    const payload = {
      probe, focused: !!document.activeElement?.matches("textarea,input,select,[contenteditable='true']"),
      standalone: matchMedia("(display-mode: standalone)").matches
        || (navigator as Navigator & {standalone?: boolean}).standalone === true,
      innerHeight: innerHeight, clientHeight: document.documentElement.clientHeight,
      visualHeight: viewport?.height ?? innerHeight, offsetTop: viewport?.offsetTop ?? 0,
      pageTop: viewport?.pageTop ?? scrollY, scrollY, scale: viewport?.scale ?? 1,
      bodyTop: body.top, bodyHeight: body.height, shellTop: shell.top, shellBottom: shell.bottom,
      composerTop: input.top, composerBottom: input.bottom, hintBottom: hint?.bottom ?? input.bottom,
      screenHeight: screen.height, screenWidth: screen.width,
    };
    lastSent = Date.now();
    if (payload.focused && Math.max(payload.innerHeight, payload.clientHeight) - payload.visualHeight >= 120) {
      try { sessionStorage.setItem(marker, "done"); } catch { /* sampling remains bounded */ }
    }
    void fetch("/api/client-viewport", {method:"POST",credentials:"same-origin",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)}).catch(() => {});
  };
  const schedule = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = window.setTimeout(sample, 1200);
  };
  const interval = window.setInterval(sample, 4000);
  window.visualViewport?.addEventListener("resize", schedule);
  window.visualViewport?.addEventListener("scroll", schedule);
  document.addEventListener("focusin", schedule);
  document.addEventListener("focusout", schedule);
  schedule();
  return () => {
    if (timer !== undefined) clearTimeout(timer);
    clearInterval(interval);
    window.visualViewport?.removeEventListener("resize", schedule);
    window.visualViewport?.removeEventListener("scroll", schedule);
    document.removeEventListener("focusin", schedule);
    document.removeEventListener("focusout", schedule);
  };
}
