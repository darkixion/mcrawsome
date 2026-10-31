// Pop-ups that the native preview surface must not cover. The surface is drawn above the page, so no
// HTML can sit on top of it; while a pop-up is open the surface is clipped to leave the pop-up's area
// to the page (native-preview.ts `rectangle`).
import { get, writable } from "svelte/store";

export const obscuring = writable<ReadonlySet<HTMLElement>>(new Set());

export function obscure(element: HTMLElement): () => void {
  obscuring.update((elements) => new Set(elements).add(element));
  return () => obscuring.update((elements) => { const next = new Set(elements); next.delete(element); return next; });
}

export function obscuringBoxes(): DOMRect[] {
  return [...get(obscuring)].filter((element) => element.isConnected).map((element) => element.getBoundingClientRect());
}
