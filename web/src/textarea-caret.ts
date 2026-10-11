/** Move by a displayed character, including emoji/combining sequences. */
export function nextCaretBoundary(text: string, position: number, direction: -1 | 1): number {
  const cursor = Math.max(0, Math.min(text.length, position));
  let previous = 0;
  const boundaries: Iterable<number> = typeof Intl.Segmenter === "function"
    ? (function* () {
        for (const segment of new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(text)) yield segment.index;
        yield text.length;
      })()
    : (function* () {
        let index = 0;
        yield index;
        for (const character of text) { index += character.length; yield index; }
      })();
  for (const boundary of boundaries) {
    if (direction === 1 && boundary > cursor) return boundary;
    if (direction === -1 && boundary >= cursor) return previous;
    previous = boundary;
  }
  return direction === 1 ? text.length : previous;
}

/** Keep a caret visible without replacing the native editor or its undo stack. */
export function revealTextareaCaret(editor: HTMLTextAreaElement, position: number): void {
  const document = editor.ownerDocument;
  const mirror = document.createElement("div");
  const marker = document.createElement("span");
  const css = getComputedStyle(editor);
  Object.assign(mirror.style, {
    position: "absolute", visibility: "hidden", pointerEvents: "none", top: "0", left: "0",
    width: `${editor.clientWidth}px`, font: css.font, lineHeight: css.lineHeight,
    letterSpacing: css.letterSpacing, padding: css.padding, boxSizing: "border-box",
    whiteSpace: "pre-wrap", overflowWrap: "break-word",
  });
  mirror.textContent = editor.value.slice(0, position);
  marker.textContent = "\u200b";
  mirror.append(marker);
  document.body.append(mirror);
  try {
    const top = marker.offsetTop;
    const line = parseFloat(css.lineHeight) || 26;
    if (top < editor.scrollTop + 8) editor.scrollTop = Math.max(0, top - 8);
    else if (top + line > editor.scrollTop + editor.clientHeight - 8) {
      editor.scrollTop = top + line - editor.clientHeight + 8;
    }
  } finally { mirror.remove(); }
}
