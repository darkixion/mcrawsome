import type { ClipSummary } from "./library";

export function displaySize(clip: ClipSummary | null, rotation: number = clip?.display_rotation ?? 0) {
  const width = clip?.width ?? 1920, height = clip?.height ?? 1080;
  return clip?.kind === "raw" && rotation % 180 !== 0
    ? { width: height, height: width } : { width, height };
}
