import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import ChartOverlay from "../src/components/ChartOverlay.svelte";
import OverlaySelector from "../src/components/OverlaySelector.svelte";
import { overlayModel, overlayProgress } from "../src/lib/preview-overlay";
const chart = { key: "iso", label: "ISO", unit: "ISO", help: "Recorded samples", series: [{ label: "ISO", points: [{ frame: 0, value: 100 }, { frame: 5, value: null }, { frame: 10, value: 100 }] }] };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("fades the unplayed trace and moves its clip backwards when seeking", async () => {
  const model = overlayModel(chart, 11);
  const view = render(ChartOverlay, { model, index: 5 });
  expect(screen.getByRole("img", { name: /ISO · ISO.*preview overlay/ })).toBeVisible();
  expect(view.container.querySelector("clipPath rect")).toHaveAttribute("width", "500");
  expect(view.container.querySelector("path")?.getAttribute("d")?.match(/M/g)).toHaveLength(2);
  expect(view.container.innerHTML).not.toMatch(/NaN|Infinity/);
  await view.rerender({ index: 2 });
  expect(view.container.querySelector("clipPath rect")).toHaveAttribute("width", "200");
  expect(overlayProgress(99, 11)).toBe(1);
  expect(overlayProgress(-1, 11)).toBe(0);
  expect(overlayProgress(0, 1)).toBe(1);
});
async function pick(name: string) {
  await fireEvent.click(screen.getByRole("button", { name: "Preview overlay" }));
  await fireEvent.click(screen.getByRole("menuitemradio", { name }));
}
it("does no work until an overlay is chosen and reuses loaded data between choices", async () => {
  const onmodel = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ frames: 11, charts: [chart], notes: [] })));
  render(OverlaySelector, { id: "a", recipe: {}, raw: true, gyro: false, count: 11, onmodel });
  expect(fetch).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole("button", { name: "Preview overlay" }));
  expect(screen.queryByRole("menuitemradio", { name: /^Gyro$/ })).toBeNull();
  expect(screen.getByRole("menuitemradio", { name: "No overlay" })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("menuitemradio", { name: "ISO" }).querySelector("svg")).not.toBeNull();
  await fireEvent.click(screen.getByRole("button", { name: "Preview overlay" }));
  await pick("ISO");
  await waitFor(() => expect(onmodel).toHaveBeenLastCalledWith(expect.objectContaining({ frames: 11 })));
  expect(screen.getByRole("img", { name: "About this overlay: Recorded samples" })).toHaveAttribute("title", "Recorded samples");
  expect(screen.queryByText(/Min \/ max/)).toBeNull();
  await pick("No overlay");
  await waitFor(() => expect(onmodel).toHaveBeenLastCalledWith(null));
  await pick("ISO");
  expect(fetch).toHaveBeenCalledTimes(1);
});
it("cancels stale reads and clears an overlay when the clip changes", async () => {
  let signal: AbortSignal | undefined;
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => { signal = init.signal; return new Promise<Response>(() => {}); }));
  const onmodel = vi.fn();
  const view = render(OverlaySelector, { id: "a", recipe: {}, raw: true, gyro: true, count: 11, onmodel });
  await pick("Gyro");
  const old = signal;
  await view.rerender({ id: "b" });
  expect(old?.aborted).toBe(true);
  expect(onmodel).toHaveBeenLastCalledWith(null);
});
