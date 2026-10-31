import { writable } from "svelte/store";
import type { Box } from "./dng-crop";

// The mounted DNG crop being placed on the preview: the settings panel starts and applies it, and the
// preview shows the whole frame with this rectangle on it. `box` is in the shown picture's full-size
// pixels; `aspect` (width / height) holds the shape while resizing, or null for any shape.
export type CropEdit = { id: string; box: Box; aspect: number | null };
export const cropEdit = writable<CropEdit | null>(null);

/** What the clip screen offers the settings panel for placing the crop; `whole` is the shown frame's size,
 * `saved` the saved crop as shown, and `block` the step every edge snaps to. */
export type CropTools = {
  readonly whole: { width: number; height: number }; readonly saved: Box | null; readonly block: number;
  start: () => void; set: (box: Box) => void; shape: (aspect: number | null) => void; apply: () => void; cancel: () => void;
};
