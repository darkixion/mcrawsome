import { get } from "svelte/store";
import { api, type ApiError, type Schemas } from "./api";
import { mediaView, showOpened } from "./library";
import { selection } from "./selection";
import { isSession, sessionRequest } from "./session-request";

/** File-manager activation is global, including when Settings or a saved session is on screen. A session
 * file goes to the session bar, which loads it as its own Load does; recordings opened with it join it. */
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
  function load(path: string, paths: string[]): void {
    sessionRequest.set({ path, paths });
    window.location.hash = "#/library";
  }
  const opened = (event: Event) => {
    const paths = (event as CustomEvent<unknown>).detail;
    if (!Array.isArray(paths) || !paths.length || paths.length > 256 || !paths.every(path => typeof path === "string")) return;
    const session = paths.filter(isSession).at(-1);  // the last one given
    if (session !== undefined) load(session, paths.filter(path => !isSession(path)));
    else enqueue(paths);
  };
  window.addEventListener("mcrawsome-open-files", opened);
  const url = new URL(window.location.href);
  const cold = url.searchParams.get("open") === "1";
  const session = url.searchParams.get("session");  // one the engine could not load at launch
  if (cold || session !== null) {
    url.searchParams.delete("open");
    url.searchParams.delete("session");
    window.history.replaceState(null, "", url);
  }
  if (cold) enqueue(null);
  if (session) load(session, []);
  return () => { active = false; window.removeEventListener("mcrawsome-open-files", opened); };
}
