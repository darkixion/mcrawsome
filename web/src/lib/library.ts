// The clips in the interface's folders: read once from GET /library, then kept current by the
// `library` events on the event stream (lib/events.ts), so a new clip appears without a reload.
import { get, writable } from "svelte/store";
import { api, type ApiError, type Schemas } from "./api";
import { selection } from "./selection";

export type ClipSummary = Schemas["ClipSummary"];
export type ClipDetails = Schemas["ClipDetails"];
/** `event: library` (the engine's LibraryChange; event bodies are not in the OpenAPI document). */
export type LibraryChange = {
  clips: ClipSummary[];
  removed: string[];
  removed_paths: string[];
  missing_folders: string[];
  /** The engine dropped changes for this client: read the whole library again. */
  reload?: boolean;
};

export const libraryChanges = writable<LibraryChange | null>(null);

export const clipViews = writable<Record<string, Schemas["ClipView"]>>({});
export function setClipView(id: string, patch: Partial<Schemas["ClipView"]>): void {
  clipViews.update(views => ({ ...views, [id]: { overlay: "off", resolution: "adaptive", ...views[id], ...patch } }));
}

export const mediaView = writable<"library" | "opened" | null>(null);
// The selected clip's preview where the clip list is, with its settings beside it: remembered in this browser.
const DOCKED = "mcrawsome.preview-docked";
export const previewDocked = writable<boolean>((() => { try { return localStorage.getItem(DOCKED) === "1"; } catch { return false; } })());
previewDocked.subscribe((docked) => { try { localStorage.setItem(DOCKED, docked ? "1" : "0"); } catch { /* a convenience */ } });
export const openedPaths = writable<string[]>([]);
export const openedClips = writable<ClipSummary[]>([]);
export const clips = writable<ClipSummary[] | null>(null); // null until the first load
export const missingFolders = writable<string[]>([]);
export const libraryProblem = writable<ApiError | null>(null);

/** A clip's key: its id, or its path while it has none (copying or unreadable). */
export function clipKey(clip: ClipSummary): string {
  return clip.id ?? `path:${clip.path}`;
}

export function thumbnailUrl(id: string): string {
  return `/api/v1/clips/${encodeURIComponent(id)}/thumbnail`;
}

export const thumbnailRevisions = writable<Record<string, number>>({});
export function refreshThumbnail(id: string): void {
  thumbnailRevisions.update(values => ({ ...values, [id]: (values[id] ?? 0) + 1 }));
}

/** The list after a change: drop the removed ids and id-less paths, then add or replace its clips. */
export function applyChange(list: ClipSummary[], change: LibraryChange): ClipSummary[] {
  const removed = new Set(change.removed);
  const removedPaths = new Set(change.removed_paths);
  const changed = new Map(change.clips.map((clip) => [clipKey(clip), clip]));
  const named = new Set(change.clips.filter((clip) => clip.id !== null).map((clip) => clip.path));
  const kept = list.filter((clip) => {
    if (clip.id !== null) return !removed.has(clip.id);
    return !removedPaths.has(clip.path) && !named.has(clip.path); // a copied clip now has an id
  });
  const replaced = kept.map((clip) => changed.get(clipKey(clip)) ?? clip);
  const known = new Set(kept.map(clipKey));
  return [...replaced, ...change.clips.filter((clip) => !known.has(clipKey(clip)))];
}

let pending: LibraryChange[] | null = null; // changes that arrive while a load is under way
let load = 0;

function pruneSelection(): void {
  const ids = new Set([...(get(clips) ?? []), ...get(openedClips)].map((clip) => clip.id));
  if (get(selection).some((id) => !ids.has(id))) selection.update((chosen) => chosen.filter((id) => ids.has(id)));
}

function show(list: ClipSummary[]): void { clips.set(list); pruneSelection(); }
export function showOpened(list: ClipSummary[], paths?: string[]): void {
  openedPaths.set(paths ?? [...new Set([...get(openedPaths), ...list.map(clip => clip.path)])]);
  openedClips.set(list); pruneSelection();
}

/** Read the library from the engine (which rescans its folders). */
export async function loadLibrary(): Promise<void> {
  const request = ++load;
  pending = [];
  const result = await api.library();
  if (request !== load) return;
  const missed = pending;
  pending = null;
  if (!result.ok) { libraryProblem.set(result); return; }
  libraryProblem.set(null);
  // A change sent while the list was on its way may be newer than the list: apply it again.
  show(missed.reduce(applyChange, result.data.clips));
  missingFolders.set(missed.at(-1)?.missing_folders ?? result.data.missing_folders);
}

/** Load again if the library has been shown (after the event stream was away). */
export function reloadLibrary(): void {
  if (get(clips) !== null) void loadLibrary();
}

/** Apply one `event: library`. */
export function receive(change: LibraryChange): void {
  libraryChanges.set(change);
  if (change.reload) {
    missingFolders.set(change.missing_folders);
    void loadLibrary();
    return;
  }
  pending?.push(change);
  const list = get(clips);
  if (list !== null) show(applyChange(list, change));
  missingFolders.set(change.missing_folders);
}
