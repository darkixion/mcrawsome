import { get } from "svelte/store";
import { afterEach, expect, it, vi } from "vitest";
import { forgetSkims, skimKey, skims, wantSkim } from "../src/lib/skims";

afterEach(() => { forgetSkims(); vi.unstubAllGlobals(); });

function engine(): { asked: string[]; finish: (id: string) => void } {
  const asked: string[] = [], waiting = new Map<string, () => void>();
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const id = /clips\/([^/]+)\/skim/.exec(String(url))![1]!;
    asked.push(id);
    return new Promise<Response>(resolve => waiting.set(id, () => resolve(Response.json({ frames: [`${id}0`, `${id}1`] }))));
  }));
  return { asked, finish: (id: string) => waiting.get(id)!() };
}

it("asks for one clip's skim at a time, then only the latest clip the pointer rested on", async () => {
  const { asked, finish } = engine();
  wantSkim("a");
  wantSkim("b"); // the pointer moved on while the engine made a's
  wantSkim("c");
  expect(asked).toEqual(["a"]);
  finish("a");
  await vi.waitFor(() => expect(asked).toEqual(["a", "c"]));
  finish("c");
  await vi.waitFor(() => expect(get(skims)[skimKey("c")]).toEqual(["data:image/jpeg;base64,c0", "data:image/jpeg;base64,c1"]));
  expect(get(skims)[skimKey("a")]).toHaveLength(2);
  wantSkim("a"); // remembered: not asked again
  expect(asked).toEqual(["a", "c"]);
});

it("asks again after the clip's settings change its thumbnail", async () => {
  const { asked, finish } = engine();
  wantSkim("a");
  finish("a");
  await vi.waitFor(() => expect(get(skims)[skimKey("a")]).toBeDefined());
  wantSkim("a", 1);
  expect(asked).toEqual(["a", "a"]);
});
