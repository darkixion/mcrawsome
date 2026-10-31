// The mounted DNG crop (develop.mount_crop), as the engine reads it: `WxH+L+T` in recorded sensor pixels,
// before the saved orientation turns the picture (mcrawsome/develop/crop.py). The editor works on the picture
// as shown, so rectangles are turned between the two here, and snapped so that every edge keeps the
// Bayer phase and lands on a whole proxy pixel (2 x the proxy factor, mount/sequence.py).

export type Box = { left: number; top: number; width: number; height: number };

export const PROXY_FACTORS: Record<string, number> = { full: 1, half: 2, quarter: 4, eighth: 8 };
export const ASPECTS: { label: string; value: number | null | "recorded" }[] = [
  { label: "Free", value: null }, { label: "Recorded", value: "recorded" }, { label: "16:9", value: 16 / 9 },
  { label: "17:9 (DCI)", value: 256 / 135 }, { label: "2.39:1", value: 2.39 }, { label: "4:3", value: 4 / 3 },
  { label: "1:1", value: 1 }, { label: "4:5", value: 4 / 5 }, { label: "9:16", value: 9 / 16 },
];

export function parseCrop(text: unknown): Box | null {
  const found = typeof text === "string" ? /^(\d+)x(\d+)(?:\+(\d+)\+(\d+))?$/.exec(text) : null;
  return found ? { width: Number(found[1]), height: Number(found[2]), left: Number(found[3] ?? 0), top: Number(found[4] ?? 0) } : null;
}

export function formatCrop(box: Box): string {
  return box.left || box.top ? `${box.width}x${box.height}+${box.left}+${box.top}` : `${box.width}x${box.height}`;
}

/** The shown picture's size for a recording of `width` x `height` turned clockwise by `rotation`. */
export function shownSize(width: number, height: number, rotation: number): { width: number; height: number } {
  return rotation % 180 ? { width: height, height: width } : { width, height };
}

/** A rectangle on the shown picture, in the recorded frame's pixels. */
export function toRecorded(box: Box, rotation: number, width: number, height: number): Box {
  switch (((rotation % 360) + 360) % 360) {
    case 90: return { left: box.top, top: height - box.left - box.width, width: box.height, height: box.width };
    case 180: return { left: width - box.left - box.width, top: height - box.top - box.height, width: box.width, height: box.height };
    case 270: return { left: width - box.top - box.height, top: box.left, width: box.height, height: box.width };
    default: return { ...box };
  }
}

/** A recorded rectangle as it appears on the shown picture. */
export function toShown(box: Box, rotation: number, width: number, height: number): Box {
  const shown = shownSize(width, height, rotation);
  switch (((rotation % 360) + 360) % 360) {
    case 90: return { left: shown.width - box.top - box.height, top: box.left, width: box.height, height: box.width };
    case 180: return { left: width - box.left - box.width, top: height - box.top - box.height, width: box.width, height: box.height };
    case 270: return { left: box.top, top: shown.height - box.left - box.width, width: box.height, height: box.width };
    default: return { ...box };
  }
}

/** The nearest valid recorded crop: inside the frame, every edge on a multiple of `block`. */
export function snap(box: Box, block: number, width: number, height: number): Box {
  const floor = (value: number) => Math.floor(value / block) * block;
  const round = (value: number) => Math.round(value / block) * block;
  const w = Math.max(block, Math.min(floor(width), round(box.width)));
  const h = Math.max(block, Math.min(floor(height), round(box.height)));
  const left = Math.max(0, Math.min(floor(width - w), round(box.left)));
  const top = Math.max(0, Math.min(floor(height - h), round(box.top)));
  return { left, top, width: w, height: h };
}

/** The largest rectangle of `aspect` (width / height) centred on `box` that fits inside it. */
export function fitAspect(box: Box, aspect: number): Box {
  const width = Math.min(box.width, box.height * aspect), height = width / aspect;
  return { left: box.left + (box.width - width) / 2, top: box.top + (box.height - height) / 2, width, height };
}

export function centred(box: Box, width: number, height: number): Box {
  return { ...box, left: (width - box.width) / 2, top: (height - box.height) / 2 };
}
