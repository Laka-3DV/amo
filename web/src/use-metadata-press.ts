import { useRef, type PointerEvent, type MouseEvent } from "react";

/** A touch must begin on the control it activates. Keyboard movement can
 * otherwise retarget the editor's trailing click onto a model/effort button. */
export function useMetadataPress(mobile: boolean) {
  const metadataPressRef = useRef<HTMLButtonElement | null>(null);
  const beginMetadataPress = (event: PointerEvent<HTMLButtonElement>) => {
    metadataPressRef.current = event.currentTarget;
    if (mobile) event.preventDefault();
  };
  const activateMetadata = (event: MouseEvent<HTMLButtonElement>, action: () => void) => {
    // Keyboard and assistive activation have detail=0.
    if (mobile && event.detail > 0 && metadataPressRef.current !== event.currentTarget) return;
    metadataPressRef.current = null;
    action();
  };
  return { metadataPressRef, beginMetadataPress, activateMetadata };
}
