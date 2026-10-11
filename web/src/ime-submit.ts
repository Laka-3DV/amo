export interface SubmitKeyEvent {
  key: string;
  shiftKey: boolean;
  isComposing?: boolean;
  keyCode?: number;
}

/**
 * Mobile keyboards report keyCode 229 (or keep isComposing=true) at the
 * composition boundary even when the IME session has ended — iOS Safari does
 * this routinely after confirming a Chinese candidate with Enter. On desktop
 * the legacy 229 guard is still required (WebKit reports isComposing=false
 * with keyCode 229 mid-composition), but on touch devices 229 alone must not
 * swallow a genuine send: the user pressed Enter to send, not to confirm a
 * candidate. `isComposing` remains the authoritative IME signal on both.
 */
export function shouldSubmitTextKey(
  event: SubmitKeyEvent,
  opts?: { mobile?: boolean },
): boolean {
  return event.key === "Enter"
    && !event.shiftKey
    && event.isComposing !== true
    && (opts?.mobile === true || event.keyCode !== 229);
}

/** Coordinate keyboard and pointer submit paths around an active IME session. */
export class ImeSubmitGuard {
  private composing = false;

  startComposition(): void {
    this.composing = true;
  }

  endComposition(): void {
    this.composing = false;
  }

  shouldSubmitKey(
    event: SubmitKeyEvent,
    opts?: { mobile?: boolean },
  ): boolean {
    return !this.composing && shouldSubmitTextKey(event, opts);
  }

  /** Whether a button pointer-down should blur to commit the active IME first. */
  shouldCommitBeforeButtonSubmit(): boolean {
    return this.composing;
  }
}
