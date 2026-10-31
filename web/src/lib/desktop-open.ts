import { get } from "svelte/store";
import { api, type ApiError, type Schemas } from "./api";
import { mediaView, showOpened } from "./library";
import { selection } from "./selection";

/** File-manager activation is global, including when Settings or a saved session is on screen. */
export function connectDesktopOpen(report: (problem: ApiError | null) => void): () => void {
  let active = true;
  let pending = Promise.resolve();
  function show(data: Schemas["LibraryView"]): void {
    if (!active) return;
    if (get(mediaView) !== "opened") selection.set([]);
    showOpened(data.clips);
    mediaView.set("opened");
    window.location.hash = "#/library";
    report(null);
  }
  function enqueue(paths: string[] | null): void {
    pending = pending.then(async () => {
      if (!active) return;
      const result = paths ? await api.openClips(paths) : await api.openedClips();
      if (result.ok) show(result.data);
      else if (active) report(result);
    }).catch(() => {
      if (active) report({ ok: false, code: "input-unreadable", message: "Could not open recordings in the running window. Try opening them again." });
    });
  }
  const opened = (event: Event) => {
    const paths = (event as CustomEvent<unknown>).detail;
    if (Array.isArray(paths) && paths.length && paths.length <= 256 && paths.every(path => typeof path === "string")) enqueue(paths);
  };
  window.addEventListener("mcrawsome-open-files", opened);
  const url = new URL(window.location.href);
  if (url.searchParams.get("open") === "1") {
    url.searchParams.delete("open");
    window.history.replaceState(null, "", url);
    enqueue(null);
  }
  return () => { active = false; window.removeEventListener("mcrawsome-open-files", opened); };
}
