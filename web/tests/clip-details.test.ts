import { afterEach, expect, it, vi } from "vitest";
import { clipDetails } from "../src/lib/clip-details";

afterEach(() => vi.unstubAllGlobals());

const entry = { id: "a", name: "a.mcraw", status: "ready", badges: [] };

it("asks once for panes that want the same clip together, and again only for a changed entry", async () => {
  const fetched = vi.fn(async () => Response.json({ ...entry, width: 320 }));
  vi.stubGlobal("fetch", fetched);
  const [pane, panel] = await Promise.all([clipDetails("a", entry), clipDetails("a", { ...entry })]);
  expect(pane).toEqual(panel);
  expect(fetched).toHaveBeenCalledTimes(1);
  await clipDetails("a", structuredClone(entry)); // a Library refresh's unchanged copy
  expect(fetched).toHaveBeenCalledTimes(1);
  await clipDetails("a", { ...entry, badges: ["has-settings"] });
  expect(fetched).toHaveBeenCalledTimes(2);
});

it("shares one request between panes holding different copies of the clip", async () => {
  // The settings pane looks the clip up in the Library; an opened clip's panel has the session's copy.
  const fetched = vi.fn(async () => Response.json({ ...entry, width: 320 }));
  vi.stubGlobal("fetch", fetched);
  await Promise.all([clipDetails("a", null), clipDetails("a", { ...entry, paths: ["/opened/a.mcraw"] })]);
  expect(fetched).toHaveBeenCalledTimes(1);
  await clipDetails("a", null);
  expect(fetched).toHaveBeenCalledTimes(1);
});

it("asks again after a failure", async () => {
  const fetched = vi.fn()
    .mockResolvedValueOnce(Response.json({ error: { code: "clip-missing", message: "gone" } }, { status: 404 }))
    .mockResolvedValueOnce(Response.json({ ...entry, width: 320 }));
  vi.stubGlobal("fetch", fetched);
  expect((await clipDetails("a", entry)).ok).toBe(false);
  expect((await clipDetails("a", entry)).ok).toBe(true);
  expect(fetched).toHaveBeenCalledTimes(2);
});
