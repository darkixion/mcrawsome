import type { GroupedClip } from "./clip-groups";

export const FACETS = [
  ["camera", "Device"], ["resolution", "Resolution"], ["fps", "Frame rate"], ["iso", "ISO"],
  ["focal_length_mm", "Focal length"], ["sensor_bits", "Bit depth"], ["captured", "Date"], ["rating", "Stars"],
  ["movement", "Movement"], ["shoot", "Shoot"], ["place", "Place"], ["together", "Recorded together"],
] as const;
export type FacetKey = typeof FACETS[number][0];
export type Facets = Partial<Record<FacetKey, string>>;

/** How the phone moved, measured from its gyro log (see the engine's motion summary). */
export const MOVEMENT_LABELS: Record<string, string> = {
  still: "Still", pan: "Pan or tilt", moving: "Moving", handheld: "Handheld",
};

const DATE_RANGES: Record<string, string> = {
  "@today": "Today", "@this-week": "This week", "@this-month": "This month",
};

export function facetLabel(key: FacetKey, value: string): string {
  if (key === "captured") return Object.hasOwn(DATE_RANGES, value) ? DATE_RANGES[value]! : value;
  if (key === "rating") return value === "0" ? "Unrated" : `${value} stars`;
  if (key === "movement") return MOVEMENT_LABELS[value] ?? value;
  if (key === "together" && value === "None") return "Not with other clips";
  return value;
}

export function facetValue(clip: GroupedClip, key: FacetKey): string {
  if (key === "resolution") return clip.width != null && clip.height != null ? `${clip.width} × ${clip.height}` : "Unknown";
  if (key === "captured") return clip.captured?.slice(0, 10) ?? "Unknown";
  // The whole rate a clip is near: 23.79, 23.976 and 24 fps are all 24.
  if (key === "fps") return clip.fps == null ? "Unknown" : String(Math.round(clip.fps));
  if (key === "rating") return String(clip.rating ?? 0);
  if (key === "together") return clip.together ?? "None";
  if (key === "place") return clip.place ?? (clip.gps ? "No nearby city" : "No GPS");
  return clip[key] == null ? "Unknown" : String(clip[key]);
}
export function facetValues(clips: GroupedClip[], key: FacetKey): string[] {
  const values = [...new Set(clips.map(clip => facetValue(clip, key)))].sort((a, b) => a.localeCompare(b, "en", { numeric: true }));
  return key === "captured" ? [...Object.keys(DATE_RANGES), ...values] : values;
}

function matchesDateRange(clip: GroupedClip, range: string, now: Date): boolean {
  if (!clip.captured) return false;
  const captured = Date.parse(clip.captured);
  const start = new Date(now);
  start.setHours(0, 0, 0, 0);
  if (range === "@this-week") start.setDate(start.getDate() - (start.getDay() + 6) % 7);
  if (range === "@this-month") start.setDate(1);
  return captured >= start.getTime() && captured <= now.getTime();
}

export function matchesFacets(clip: GroupedClip, facets: Facets, now = new Date()): boolean {
  return FACETS.every(([key]) => {
    const value = facets[key];
    if (!value) return true;
    if (key === "captured" && Object.hasOwn(DATE_RANGES, value)) return matchesDateRange(clip, value, now);
    return facetValue(clip, key) === value;
  });
}
