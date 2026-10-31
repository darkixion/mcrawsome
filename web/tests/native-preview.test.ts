import { expect, it, vi } from "vitest";
import { attachNative, rectangle } from "../src/lib/native-preview";
import { PreviewClient } from "../src/lib/preview";

it("clips native content to scroll parents and preserves its original image box", () => {
  const parent = document.createElement("div");
  parent.style.overflowX = "hidden"; parent.style.overflowY = "hidden";
  const surface = document.createElement("div"); parent.append(surface); document.body.append(parent);
  parent.getBoundingClientRect = () => ({ left: 10, top: 20, right: 210, bottom: 120 }) as DOMRect;
  surface.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 300, bottom: 200, width: 300, height: 200 }) as DOMRect;
  expect(rectangle(surface)).toEqual({ x: 0, y: 0, width: 300, height: 200, left: 10, top: 20, right: 210, bottom: 120, dpr: 1 });
  parent.remove();
});

it("does not clip a fullscreen viewport to its former Library pane", () => {
  const pane = document.createElement("div"), viewport = document.createElement("div"), surface = document.createElement("div");
  pane.style.overflowX = "auto"; pane.style.overflowY = "auto";
  pane.append(viewport); viewport.append(surface); document.body.append(pane);
  pane.getBoundingClientRect = () => ({ left: 700, top: 100, right: 900, bottom: 500 }) as DOMRect;
  surface.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 1000, bottom: 600, width: 1000, height: 600 }) as DOMRect;
  Object.defineProperty(document, "fullscreenElement", { configurable: true, value: viewport });
  try { expect(rectangle(surface)).toMatchObject({ left: 0, top: 0, right: 1000, bottom: 600 }); }
  finally { Reflect.deleteProperty(document, "fullscreenElement"); pane.remove(); }
});

it("ignores a late capability reply after the viewer has closed", async () => {
  let ready!: (value: boolean) => void;
  const api = { preview_available: vi.fn(() => new Promise<boolean>((resolve) => ready = resolve)), preview_present: vi.fn(), preview_hide: vi.fn().mockResolvedValue(true), preview_bounds: vi.fn().mockResolvedValue(true) };
  const client = { nativeOutput: null } as unknown as PreviewClient;
  const connection = attachNative(client, document.createElement("div"), api);
  connection.dispose(); ready(true); await Promise.resolve();
  expect(client.nativeOutput).toBeNull();
  expect(api.preview_hide).toHaveBeenCalled();
});

it("leaves an open menu's area to the page, playing the larger rest of the picture", async () => {
  const { obscure } = await import("../src/lib/obscuring");
  const surface = document.createElement("div"), menu = document.createElement("div");
  document.body.append(surface, menu);
  surface.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 300, bottom: 200, width: 300, height: 200 }) as DOMRect;
  menu.getBoundingClientRect = () => ({ left: 10, top: 0, right: 150, bottom: 60 }) as DOMRect;
  const release = obscure(menu);
  try {
    expect(rectangle(surface)).toMatchObject({ left: 0, top: 60, right: 300, bottom: 200, height: 200 });
    menu.getBoundingClientRect = () => ({ left: 10, top: 150, right: 150, bottom: 260 }) as DOMRect;
    expect(rectangle(surface)).toMatchObject({ top: 0, bottom: 150 });
    menu.getBoundingClientRect = () => ({ left: 400, top: 0, right: 500, bottom: 60 }) as DOMRect;
    expect(rectangle(surface)).toMatchObject({ top: 0, bottom: 200 });
  } finally { release(); }
  menu.getBoundingClientRect = () => ({ left: 10, top: 0, right: 150, bottom: 60 }) as DOMRect;
  expect(rectangle(surface)).toMatchObject({ top: 0, bottom: 200 });
  surface.remove(); menu.remove();
});

it("sends the stabilised crop outline when the native picture is ready and when it changes", async () => {
  let crop: { x: number; y: number; width: number; height: number } | null = { x: 0.1, y: 0.1, width: 0.8, height: 0.8 };
  const api = { preview_available: vi.fn().mockResolvedValue(true), preview_present: vi.fn(), preview_hide: vi.fn().mockResolvedValue(true), preview_bounds: vi.fn().mockResolvedValue(true), preview_crop: vi.fn().mockResolvedValue(true) };
  const client = { nativeOutput: null } as unknown as PreviewClient;
  const connection = attachNative(client, document.createElement("div"), api, () => null, () => crop);
  await vi.waitFor(() => expect(api.preview_crop).toHaveBeenCalledWith(expect.any(Number), 1, { x: 0.1, y: 0.1, width: 0.8, height: 0.8 }));
  crop = null; connection.guide();
  expect(api.preview_crop).toHaveBeenLastCalledWith(expect.any(Number), 2, null);
  connection.dispose();
});

it("gives a pop-up as wide as the picture a band of it, keeping the full width below", async () => {
  const { obscure } = await import("../src/lib/obscuring");
  const surface = document.createElement("div"), sheet = document.createElement("div");
  document.body.append(surface, sheet);
  surface.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 300, bottom: 200, width: 300, height: 200 }) as DOMRect;
  sheet.getBoundingClientRect = () => ({ left: -1, top: 2, right: 301, bottom: 40 }) as DOMRect;
  const release = obscure(sheet);
  try { expect(rectangle(surface)).toMatchObject({ left: 0, top: 40, right: 300, bottom: 200 }); }
  finally { release(); surface.remove(); sheet.remove(); }
});
