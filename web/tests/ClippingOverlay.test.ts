import { render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import ClippingOverlay from "../src/components/ClippingOverlay.svelte";

afterEach(() => vi.unstubAllGlobals());
const result = (index: number) => Response.json({ frame_index: index, source_index: index, width: 128, height: 64, white_level: 1023, quad_bayer: false, url: "data:image/png;base64,AA==" });
it("fetches a preview-only mask only while active and hides it for playback", async () => {
  const fetcher = vi.fn().mockResolvedValue(result(1)); vi.stubGlobal("fetch", fetcher);
  const props = { id: "clip", recipe: {}, index: 1, mode: "any" as const, active: false };
  const view = render(ClippingOverlay, props);
  expect(fetcher).not.toHaveBeenCalled();
  await view.rerender({ ...props, active: true });
  expect(await screen.findByRole("img", { name: "Pink sensor-clipping overlay" })).toBeVisible();
  expect((fetcher.mock.calls[0] as [string])[0]).toBe("/api/v1/clips/clip/sensor-clipping");
  await view.rerender(props);
  expect(screen.queryByRole("img")).toBeNull();
});
it("discards an old response when the displayed frame changes", async () => {
  let finish!: (value: Response) => void;
  const fetcher = vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }))
    .mockResolvedValueOnce(result(2)); vi.stubGlobal("fetch", fetcher);
  const props = { id: "clip", recipe: {}, index: 1, mode: "all" as const, active: true };
  const view = render(ClippingOverlay, props);
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
  await view.rerender({ ...props, index: 2 });
  finish(result(1));
  expect(screen.queryByRole("img")).toBeNull();
  expect(await screen.findByRole("img")).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it("invalidates the mask when saved geometry changes on the same frame", async () => {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(result(1))); vi.stubGlobal("fetch", fetcher);
  const props = { id: "clip", recipe: {}, index: 1, mode: "any" as const, active: true };
  const view = render(ClippingOverlay, props);
  expect(await screen.findByRole("img")).toBeVisible();
  await view.rerender({ ...props, recipe: { develop: { rotation: 90 } } });
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
});
