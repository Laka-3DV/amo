import { useEffect, useLayoutEffect, useState } from "react";
import { readDeviceLayout, applyDeviceLayout } from "./responsive-layout";

/** Screen size controls space; touch capability controls Return and gestures.
 * iPad desktop-site mode and trackpads must keep the touch input contract. */
export function useDeviceLayout() {
  const [layout, setLayout] = useState(readDeviceLayout);
  useLayoutEffect(() => applyDeviceLayout(layout), [layout]);
  useEffect(() => {
    const media = window.matchMedia("(any-pointer: coarse)");
    const update = () => setLayout(previous => {
      const next = readDeviceLayout();
      return next.kind === previous.kind && next.touchInput === previous.touchInput ? previous : next;
    });
    window.addEventListener("resize", update);
    media.addEventListener("change", update);
    return () => {
      window.removeEventListener("resize", update);
      media.removeEventListener("change", update);
    };
  }, []);
  return layout;
}
