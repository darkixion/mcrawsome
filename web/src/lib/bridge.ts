import { readable } from "svelte/store";

// pywebview injects its API after the page has rendered.
export const bridge = readable<Window["pywebview"]>(undefined, (set) => {
  const ready = (): void => set(window.pywebview);
  ready();
  window.addEventListener("pywebviewready", ready);
  return () => window.removeEventListener("pywebviewready", ready);
});
