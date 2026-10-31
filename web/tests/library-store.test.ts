import { get } from "svelte/store";
import { expect, it, vi } from "vitest";
import { applyChange, clips, loadLibrary, receive, type ClipSummary } from "../src/lib/library";

const clip = (id: string | null, path: string, status: ClipSummary["status"] = "ready"): ClipSummary =>
  ({ id, path, paths: [path], name: path.split("/").at(-1)!, kind: "raw", gps: false, rating: 0, details_text: "", gyro: false, display_rotation: 0, status, badges: [] });

it("replaces a copying entry by its path once the clip has an id", () => {
  const list = [clip("a", "/c/a.mcraw"), clip(null, "/c/b.mcraw", "copying")];
  const next = applyChange(list, { clips: [clip("b", "/c/b.mcraw")], removed: [], removed_paths: [], missing_folders: [] });
  expect(next.map((item) => item.id)).toEqual(["a", "b"]);
});

it("replaces a changed clip in place and removes gone ones", () => {
  const list = [clip("a", "/c/a.mcraw"), clip("b", "/c/b.mcraw"), clip(null, "/c/x.mcraw", "unreadable")];
  const changed = { ...clip("b", "/c/b.mcraw"), badges: ["has-settings" as const] };
  const next = applyChange(list, { clips: [changed], removed: ["a"], removed_paths: ["/c/x.mcraw"], missing_folders: [] });
  expect(next).toEqual([changed]);
});

it("reloads the whole library when the engine says it dropped changes", async () => {
  const listing = (ids: string[]) =>
    new Response(JSON.stringify({ clips: ids.map((id) => clip(id, `/c/${id}.mcraw`)), missing_folders: [] }), { status: 200 });
  const fetcher = vi.fn(async () => listing(["a"]));
  vi.stubGlobal("fetch", fetcher);
  await loadLibrary();
  expect(get(clips)?.map((item) => item.id)).toEqual(["a"]);
  fetcher.mockImplementation(async () => listing(["a", "b"]));
  receive({ clips: [], removed: [], removed_paths: [], missing_folders: [], reload: true });
  await vi.waitFor(() => expect(get(clips)?.map((item) => item.id)).toEqual(["a", "b"]));
  expect(fetcher).toHaveBeenCalledTimes(2);
  vi.unstubAllGlobals();
});
