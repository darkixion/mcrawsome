import type { OverlayModel } from "./preview-overlay";
import type { DisplayFrame, NativeOutput, PreviewClient } from "./preview";
import { obscuringBoxes } from "./obscuring";

// A rectangle on the shown picture as fractions of its size, drawn by the native picture (viewport.py).
// `corners` moves a stabilised timeline's kept frame frame by frame (engine `resolve.kept_frame`);
// `stabilised` draws each frame with its kept frame filling the picture instead of outlining it.
export type CropOutline = { x: number; y: number; width: number; height: number; handles?: boolean; corners?: number[][]; stabilised?: boolean };
export type Rectangle = { x: number; y: number; width: number; height: number; left: number; top: number; right: number; bottom: number; dpr: number };
export type NativeApi = {
  preview_overlay?: (owner: number, revision: number, model: OverlayModel | null) => Promise<boolean>;
  preview_crop?: (owner: number, revision: number, outline: CropOutline | null) => Promise<boolean>;
  preview_available: (owner: number) => Promise<boolean>;
  preview_present: (owner: number, epoch: number, display: DisplayFrame, rect: Rectangle, index: number) => Promise<boolean | null>;
  preview_bounds: (owner: number, epoch: number, rect: Rectangle) => Promise<boolean>;
  preview_hide: (owner: number, epoch: number) => Promise<boolean>;
};

let owner = 0;
export function rectangle(surface: HTMLElement): Rectangle | null {
  const box = surface.getBoundingClientRect();
  const fullscreen = document.fullscreenElement ?? (document as Document & { webkitFullscreenElement?: Element }).webkitFullscreenElement;
  let left = Math.max(0, box.left), top = Math.max(0, box.top);
  let right = Math.min(window.innerWidth, box.right), bottom = Math.min(window.innerHeight, box.bottom);
  for (let parent = surface.parentElement; parent; parent = parent.parentElement) {
    const style = getComputedStyle(parent), bounds = parent.getBoundingClientRect();
    if (/(auto|scroll|hidden|clip)/.test(style.overflowX || style.overflow)) {
      left = Math.max(left, bounds.left); right = Math.min(right, bounds.right);
    }
    if (/(auto|scroll|hidden|clip)/.test(style.overflowY || style.overflow)) {
      top = Math.max(top, bounds.top); bottom = Math.min(bottom, bounds.bottom);
    }
    if (parent === fullscreen) break;
  }
  // An open pop-up over the picture keeps its area: the picture plays on in the larger part above or below.
  for (const box of obscuringBoxes()) {
    if (box.right <= left || box.left >= right || box.bottom <= top || box.top >= bottom) continue;
    if (box.top - top >= bottom - box.bottom) bottom = Math.min(bottom, box.top);
    else top = Math.max(top, box.bottom);
  }
  if (right <= left || bottom <= top) return null;
  return { x: box.x, y: box.y, width: box.width, height: box.height, left, top, right, bottom, dpr: window.devicePixelRatio || 1 };
}

export function attachNative(client: PreviewClient, surface: HTMLElement, api: NativeApi, overlay: () => OverlayModel | null = () => null, crop: () => CropOutline | null = () => null): { chart: () => void; guide: () => void; move: () => void; dispose: () => void } {
  const identity = owner = Math.max(owner + 1, Date.now() * 1024);
  let active = true, epoch = 0, visible = false, scheduled = 0;
  let chartRevision = 0;
  function chart() { if (active) void api.preview_overlay?.(identity, ++chartRevision, overlay()).catch(() => {}); }
  let cropRevision = 0;
  function guide() { if (active) void api.preview_crop?.(identity, ++cropRevision, crop()).catch(() => {}); }
  const output: NativeOutput = {
    async paint(header, generation) {
      epoch = generation;
      const rect = rectangle(surface);
      if (!active || !header.display || !rect) return false;
      visible = true;
      return api.preview_present(identity, epoch, header.display, rect, header.index!);
    },
    hide(generation) {
      epoch = generation; visible = false;
      void api.preview_hide(identity, epoch).catch(() => {});
    },
  };
  void api.preview_available(identity).then((available) => {
    if (active && available) { chart(); guide(); client.nativeOutput = output; }
  }).catch(() => {});
  function move() {
    if (!active || !visible || scheduled) return;
    scheduled = requestAnimationFrame(() => {
      scheduled = 0;
      const rect = rectangle(surface);
      if (rect) void api.preview_bounds(identity, epoch, rect).catch(() => {});
      else output.hide(epoch);
    });
  }
  return { chart, guide, move, dispose() {
    active = false; cancelAnimationFrame(scheduled);
    if (client.nativeOutput === output) client.nativeOutput = null;
    output.hide(Number.MAX_SAFE_INTEGER);
  } };
}
