import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import ClipAnalytics from "../src/components/ClipAnalytics.svelte";
import { timingPlot } from "../src/lib/diagnostic-plots";
import { ANALYTICS_VIEWS } from "../src/lib/analytics-catalogue";

const timing = { frames: 3, intervals: 2, median_ms: 40, minimum_ms: 40, maximum_ms: 40, dropped: 0, duplicated: 0, nonpositive: 0, buckets: [{ first_frame: 1, last_frame: 2, intervals: 2, minimum_ms: 40, maximum_ms: 40, mean_ms: 40 }] };
const report = { frames: 3, sampled_frames: 3, unreadable_frames: 0, timing, timing_error: null, series: [
  { key: "iso", label: "ISO", unit: "ISO", help: "Recorded sensor sensitivity", points: [{ frame: 0, value: 100 }, { frame: 1, value: 200 }, { frame: 2, value: 300 }] },
  { key: "focus", label: "Focus distance", unit: "dioptres", help: "Zero means infinity", points: [{ frame: 0, value: 0 }, { frame: 1, value: null }, { frame: 2, value: .5 }] },
] };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("shows timing and capture graphs for RAW with no gyro and switches units", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ready", report })));
  render(ClipAnalytics, { id: "a", name: "a.mcraw", raw: true, gyro: false });
  expect(await screen.findByRole("img", { name: "Recorded frame intervals" })).toBeVisible();
  await fireEvent.change(screen.getByRole("combobox", { name: "Analytics graph" }), { target: { value: "iso" } });
  expect(screen.getByRole("img", { name: "Recorded ISO" })).toBeVisible();
  expect(screen.getByText(/Every recorded frame included/)).toBeVisible();
  await fireEvent.change(screen.getByRole("combobox", { name: "Analytics graph" }), { target: { value: "focus" } });
  expect(screen.getByRole("img", { name: "Recorded Focus distance" })).toBeVisible();
  expect(screen.getByText(/Zero means infinity/)).toBeVisible();
  expect(vi.mocked(fetch).mock.calls).toHaveLength(1);
});

it("serialises capture and gyro requests against the shared worker", async () => {
  let finish!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("/analytics-scan") ? new Promise<Response>(resolve => { finish = resolve; }) : Response.json({ samples: 2, duration_s: 1, sample_rate_hz: 1, acceleration: null, angular_velocity: [{ start_s: 0, end_s: 1, minimum: [0,0,0], maximum: [1,1,1], mean: [.5,.5,.5] }] })));
  render(ClipAnalytics, { id: "a", name: "a.mcraw", raw: true, gyro: true });
  await screen.findByText("Waiting for background metadata…");
  expect(vi.mocked(fetch).mock.calls).toHaveLength(1);
  finish(Response.json({ status: "ready", report }));
  await screen.findByRole("combobox", { name: "Analytics graph" });
  expect(fetch).toHaveBeenCalledTimes(1);
  await fireEvent.change(screen.getByRole("combobox", { name: "Analytics graph" }), { target: { value: "gyro" } });
  expect(await screen.findByRole("img", { name: "Recorded gyro angular velocity" })).toBeVisible();
  expect(vi.mocked(fetch).mock.calls.map(([url]) => url)).toEqual(["/api/v1/clips/a/analytics-scan", "/api/v1/clips/a/gyro"]);
});

it("cancels pending reads and clears previous graphs when the clip changes", async () => {
  let finish!: (value: Response) => void;
  let signal: AbortSignal | undefined;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes("/b/")) { signal = init?.signal as AbortSignal; return new Promise<Response>(resolve => { finish = resolve; }); }
    return Response.json({ status: "ready", report });
  }));
  const view = render(ClipAnalytics, { id: "a", name: "a.mcraw", raw: true, gyro: false });
  await screen.findByRole("combobox", { name: "Analytics graph" });
  await fireEvent.change(screen.getByRole("combobox", { name: "Analytics graph" }), { target: { value: "iso" } });
  await screen.findByRole("img", { name: "Recorded ISO" });
  await view.rerender({ id: "b", name: "b.mcraw" });
  await screen.findByText("Waiting for background metadata…");
  expect(screen.queryByRole("img", { name: "Recorded ISO" })).toBeNull();
  view.unmount();
  expect(signal?.aborted).toBe(true);
  finish(Response.json({ status: "ready", report }));
});

it("loads LOG analytics while playback continues without a mount", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ready", report: { ...report, series: [], notes: ["No RAW coefficients were recorded"] } })));
  render(ClipAnalytics, { id: "b", name: "b.mp4", raw: false, gyro: false });
  expect(await screen.findByRole("img", { name: "Recorded frame intervals" })).toBeVisible();
  expect(screen.getByText(/No RAW coefficients/)).toBeVisible();
  expect(vi.mocked(fetch).mock.calls).toHaveLength(1);
});

it("polls background progress through playback and stops watching when unmounted", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "scanning", completed: 32, total: 1000 })));
  const view = render(ClipAnalytics, { id: "a", name: "a.mcraw", raw: true, gyro: false });
  expect(await screen.findByText(/32 \/ 1000 frames/)).toBeVisible();
  expect(screen.getByRole("progressbar", { name: "Metadata scan progress" })).toHaveAttribute("value", "32");
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2), { timeout: 2000 });
  view.unmount();
  const count = vi.mocked(fetch).mock.calls.length;
  await new Promise(resolve => setTimeout(resolve, 850));
  expect(fetch).toHaveBeenCalledTimes(count);
});

it("zooms constant timing safely and retains the recorded frame endpoints", () => {
  const plot = timingPlot(timing, true);
  expect(plot.low).toBeLessThan(40);
  expect(plot.high).toBeGreaterThan(40);
  expect(plot.y(40)).toBeCloseTo(60);
  expect(plot.path).not.toMatch(/NaN|Infinity/);
  expect(plot.x(1)).toBe(30);
  expect(plot.x(2)).toBe(290);
  expect(timingPlot(timing).low).toBe(0);
});

it("groups actual white-balance coefficients and offers lazy four-channel shading maps", async () => {
  const series = ["R", "G", "B"].map((label, i) => ({ key: `neutral${i}`, group: "wb", label, unit: "coefficient", help: "Actual neutral", samples: 3, minimum: .5, maximum: 1, points: [{ frame: 0, value: .5, minimum: .5, maximum: .5 }, { frame: 2, value: 1, minimum: 1, maximum: 1 }] }));
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.includes("shading-map") ? Response.json({ frame: 30, width: 2, height: 1, channels: [[1,2],[2,3],[3,4],[4,5]] }) : Response.json({ status: "ready", report: { ...report, series, shading_frames: [0,30], shading_step: 30 } })));
  render(ClipAnalytics, { id: "a", name: "a.mcraw", raw: true, gyro: false });
  await screen.findByRole("combobox", { name: "Analytics graph" });
  await fireEvent.change(screen.getByRole("combobox", { name: "Analytics graph" }), { target: { value: "wb" } });
  expect(await screen.findByRole("img", { name: "Recorded White balance" })).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(1);
  await fireEvent.change(screen.getByRole("combobox", { name: "Analytics graph" }), { target: { value: "heatmap" } });
  expect(await screen.findByRole("img", { name: /R lens shading map/ })).toBeVisible();
  await fireEvent.change(screen.getByRole("combobox", { name: "Shading channel" }), { target: { value: "3" } });
  expect(await screen.findByRole("img", { name: /B lens shading map/ })).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(2); // changing channels reuses this frame's map
});


it("offers every overlay graph in Analytics and retains extra RAW views", async () => {
  const series = ANALYTICS_VIEWS.filter(view => view.source === "raw").map(view => ({
    key: view.key, group: ["black", "white"].includes(view.key) ? "levels" : view.key,
    label: view.label, unit: "units", help: "Recorded measurement", points: [{ frame: 0, value: 1 }],
  }));
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ready", report: { ...report, series, shading_frames: [0] } })));
  render(ClipAnalytics, { id: "a", name: "a.mcraw", raw: true, gyro: true });
  const menu = await screen.findByRole("combobox", { name: "Analytics graph" });
  expect(Array.from(menu.querySelectorAll("option")).map(option => option.value)).toEqual([...ANALYTICS_VIEWS.map(view => view.key), "levels", "heatmap"]);
  for (const key of ["black", "white", "levels"]) {
    await fireEvent.change(menu, { target: { value: key } });
    const label = key === "levels" ? "RAW black/white levels" : ANALYTICS_VIEWS.find(view => view.key === key)!.label;
    expect(screen.getByRole("img", { name: `Recorded ${label}` })).toBeVisible();
  }
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("switches Gyro and Acceleration without rereading the recording", async () => {
  const buckets = [{ start_s: 0, end_s: 1, frame: 1, minimum: [0,0,0], maximum: [1,1,1], mean: [.5,.5,.5] }];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => Response.json(url.endsWith("/gyro")
    ? { samples: 2, duration_s: 1, sample_rate_hz: 1, angular_velocity: buckets, acceleration: buckets }
    : { status: "ready", report })));
  render(ClipAnalytics, { id: "a", name: "a.mcraw", raw: true, gyro: true });
  const menu = await screen.findByRole("combobox", { name: "Analytics graph" });
  await fireEvent.change(menu, { target: { value: "acceleration" } });
  expect(await screen.findByRole("img", { name: "Recorded acceleration" })).toBeVisible();
  await fireEvent.change(menu, { target: { value: "gyro" } });
  expect(await screen.findByRole("img", { name: "Recorded gyro angular velocity" })).toBeVisible();
  await fireEvent.change(menu, { target: { value: "timing" } });
  expect(screen.getByRole("img", { name: "Recorded frame intervals" })).toBeVisible();
  await fireEvent.change(menu, { target: { value: "acceleration" } });
  expect(await screen.findByRole("img", { name: "Recorded acceleration" })).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(2);
});


it("keeps gyro available when the capture scan fails and marks missing acceleration", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => Response.json(url.endsWith("/gyro")
    ? { samples: 2, duration_s: 1, sample_rate_hz: 1, acceleration: null, angular_velocity: [{ start_s: 0, end_s: 1, minimum: [0,0,0], maximum: [1,1,1], mean: [.5,.5,.5] }] }
    : { status: "failed", message: "Timing index unavailable" })));
  render(ClipAnalytics, { id: "a", name: "a.mp4", raw: false, gyro: true });
  expect(await screen.findByRole("img", { name: "Recorded gyro angular velocity" })).toBeVisible();
  expect(screen.getByText("Timing index unavailable")).toBeVisible();
  expect(screen.getByRole("option", { name: "Acceleration (not recorded)" })).toBeDisabled();
});
