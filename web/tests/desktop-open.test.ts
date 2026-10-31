import { get } from "svelte/store";
import { waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../src/lib/api";
import { connectDesktopOpen } from "../src/lib/desktop-open";
import { mediaView, openedClips, type ClipSummary } from "../src/lib/library";
import { selection } from "../src/lib/selection";
import { settings } from "../src/lib/settings";
import { sessionRequest } from "../src/lib/session-request";

let stop = () => {};
const clip = (id: string) => ({ id, name: `${id}.mcraw`, path: `/outside/${id}.mcraw` }) as ClipSummary;
const view = (...ids: string[]) => ({ clips: ids.map(clip), missing_folders: [] });
beforeEach(() => { mediaView.set("library"); openedClips.set([]); selection.set(["library-clip"]); sessionRequest.set(null); window.history.replaceState(null, "", "/#/settings"); });
afterEach(() => { stop(); vi.restoreAllMocks(); });

it("adds file-manager paths to Open clips from any screen without changing library folders", async () => {
  const saved = { folders: ["/library"] } as never;
  settings.set(saved);
  vi.spyOn(api, "openClips").mockResolvedValue({ ok: true, data: view("existing", "one", "two") });
  const report = vi.fn();
  stop = connectDesktopOpen(report);
  window.dispatchEvent(new CustomEvent("mcrawsome-open-files", { detail: ["/outside/one.mcraw", "/outside/two.mcraw"] }));
  await waitFor(() => expect(get(openedClips)).toHaveLength(3));
  expect(api.openClips).toHaveBeenCalledWith(["/outside/one.mcraw", "/outside/two.mcraw"]);
  expect(get(mediaView)).toBe("opened");
  expect(get(selection)).toEqual([]);
  expect(window.location.hash).toBe("#/library");
  expect(get(settings)).toBe(saved);
  expect(report).toHaveBeenLastCalledWith(null);
});

it("queues repeated desktop activations rather than dropping them during an earlier open", async () => {
  let finish: (value: Awaited<ReturnType<typeof api.openClips>>) => void = () => {};
  const open = vi.spyOn(api, "openClips")
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockResolvedValueOnce({ ok: true, data: view("one", "two") });
  stop = connectDesktopOpen(() => {});
  window.dispatchEvent(new CustomEvent("mcrawsome-open-files", { detail: ["/one.mcraw"] }));
  window.dispatchEvent(new CustomEvent("mcrawsome-open-files", { detail: ["/two.mcraw"] }));
  await waitFor(() => expect(open).toHaveBeenCalledTimes(1));
  finish({ ok: true, data: view("one") });
  await waitFor(() => expect(open).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(get(openedClips).map(clip => clip.id)).toEqual(["one", "two"]));
});

it("opens cold-launch references in Open clips", async () => {
  mediaView.set("opened");
  window.history.replaceState(null, "", "/?open=1#/library");
  vi.spyOn(api, "openedClips").mockResolvedValue({ ok: true, data: view("one", "two") });
  const update = vi.spyOn(api, "updateSession");
  stop = connectDesktopOpen(() => {});
  await waitFor(() => expect(get(openedClips)).toHaveLength(2));
  expect(get(mediaView)).toBe("opened");
  expect(window.location.search).toBe("");
  expect(update).not.toHaveBeenCalled();
});

it("keeps the current workspace on errors and stops receiving when the page closes", async () => {
  const failure = { ok: false as const, code: "input-unreadable", message: "Recording is unavailable" };
  const open = vi.spyOn(api, "openClips").mockResolvedValue(failure);
  const report = vi.fn();
  stop = connectDesktopOpen(report);
  window.dispatchEvent(new CustomEvent("mcrawsome-open-files", { detail: ["/missing.mcraw"] }));
  await waitFor(() => expect(report).toHaveBeenCalledWith(failure));
  expect(window.location.hash).toBe("#/settings");
  expect(get(mediaView)).toBe("library");
  stop();
  window.dispatchEvent(new CustomEvent("mcrawsome-open-files", { detail: ["/another.mcraw"] }));
  await Promise.resolve();
  expect(open).toHaveBeenCalledOnce();
});

it("hands a session file to the session bar, with the recordings opened alongside it", () => {
  const open = vi.spyOn(api, "openClips");
  stop = connectDesktopOpen(() => {});
  window.dispatchEvent(new CustomEvent("mcrawsome-open-files", { detail: ["/clips/a.mcraw", "/clips/Shoot.MCRAWSOME-SESSION.JSON"] }));
  expect(get(sessionRequest)).toEqual({ path: "/clips/Shoot.MCRAWSOME-SESSION.JSON", paths: ["/clips/a.mcraw"] });
  expect(window.location.hash).toBe("#/library");
  expect(open).not.toHaveBeenCalled();
});

it("passes a session the engine could not load at launch to the session bar to explain", async () => {
  window.history.replaceState(null, "", "/?open=1&session=%2Fclips%2Fbad.mcrawsome-session.json#/library");
  vi.spyOn(api, "openedClips").mockResolvedValue({ ok: true, data: view() });
  stop = connectDesktopOpen(() => {});
  expect(get(sessionRequest)).toEqual({ path: "/clips/bad.mcrawsome-session.json", paths: [] });
  expect(window.location.search).toBe("");
  await waitFor(() => expect(api.openedClips).toHaveBeenCalled());
});
