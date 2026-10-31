// Clips grouped by when and where they were recorded, from the Library's facts alone.
// A shoot is a run of clips recorded within two hours of each other (and within 5 km, when both
// recorded a GPS fix). Clips recorded together overlap in time: one phone records one clip at a
// time, so overlapping clips come from different devices and can be synced by timecode.
import type { ClipSummary } from "./library";

export const SHOOT_GAP_S = 2 * 3600;
export const SHOOT_DISTANCE_KM = 5;
export type GroupedClip = ClipSummary & { shoot?: string | null; together?: string | null };

const key = (clip: ClipSummary): string => clip.id ?? `path:${clip.path}`;
const start = (clip: ClipSummary): number | null => (clip.captured ? Date.parse(clip.captured) : null);
const end = (clip: ClipSummary): number | null => {
  const from = start(clip);
  return from === null ? null : from + Math.max(0, clip.duration_s ?? 0) * 1000;
};

export function distanceKm(a: [number, number], b: [number, number]): number {
  const radians = (value: number) => (value * Math.PI) / 180;
  const [lat1, lon1, lat2, lon2] = [a[0], a[1], b[0], b[1]].map(radians) as [number, number, number, number];
  const h = Math.sin((lat2 - lat1) / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin((lon2 - lon1) / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(h)));
}

function when(time: number): string {
  return new Date(time).toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** Each timed clip's shoot label: the first clip's date and time, and its place when known. */
export function shoots(clips: ClipSummary[]): Map<string, string> {
  const timed = clips.filter(clip => start(clip) !== null).sort((a, b) => start(a)! - start(b)!);
  const labels = new Map<string, string>();
  let group: ClipSummary[] = [];
  const close = () => {
    if (!group.length) return;
    const place = group.find(clip => clip.place)?.place;
    const label = `${when(start(group[0]!)!)}${place ? ` · ${place}` : ""}`;
    for (const clip of group) labels.set(key(clip), label);
    group = [];
  };
  for (const clip of timed) {
    const previous = group.at(-1);
    const apart = previous !== undefined && (start(clip)! - end(previous)! > SHOOT_GAP_S * 1000
      || (!!previous.position && !!clip.position && distanceKm(previous.position, clip.position) > SHOOT_DISTANCE_KM));
    if (apart) close();
    group.push(clip);
  }
  close();
  return labels;
}

/** A label for each clip whose recording overlaps another's: the group's start and its size. */
export function recordedTogether(clips: ClipSummary[]): Map<string, string> {
  const timed = clips.filter(clip => start(clip) !== null).sort((a, b) => start(a)! - start(b)!);
  const labels = new Map<string, string>();
  let group: ClipSummary[] = [], reach = -Infinity;
  const close = () => {
    if (group.length > 1) {
      const label = `${when(start(group[0]!)!)}, ${group.length} clips`;
      for (const clip of group) labels.set(key(clip), label);
    }
    group = [];
  };
  for (const clip of timed) {
    if (start(clip)! >= reach) { close(); reach = -Infinity; }
    group.push(clip);
    reach = Math.max(reach, end(clip)!);
  }
  close();
  return labels;
}

/** The clips with their shoot and recorded-together labels, for filtering. */
export function withGroups<T extends ClipSummary>(clips: T[]): (T & GroupedClip)[] {
  const shot = shoots(clips), together = recordedTogether(clips);
  return clips.map(clip => ({ ...clip, shoot: shot.get(key(clip)) ?? null, together: together.get(key(clip)) ?? null }));
}
