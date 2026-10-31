import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import AnalyticsChart from "../src/components/AnalyticsChart.svelte";
import { niceScale, nearestSample, chartPath, chartTick } from "../src/lib/analytics-chart";

afterEach(cleanup);
const lines = ["X", "Y", "Z"].map((label, i) => ({ label, colour: ["#68c7cb", "#e8ae63", "#b29cfa"][i]!, unit: "°/s", samples: [
  { x: .2, value: i + .125, minimum: i - .25, maximum: i + .5, frame: 7 },
  { x: .6, value: i + 10.125, minimum: i + 10, maximum: i + 11, frame: 17 },
] }));
it("shows incremented axes and all three values with a time/frame cursor on hover", async () => {
  const view = render(AnalyticsChart, { label: "Gyro", lines, xMin: 0, xMax: 1, xUnit: "seconds", yLabel: "Angular velocity (°/s)" });
  const svg = screen.getByRole("img", { name: "Gyro" });
  vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 520, height: 228 } as DOMRect);
  expect(svg.querySelectorAll(".y-tick").length).toBeGreaterThanOrEqual(4);
  expect(svg.querySelectorAll(".x-tick").length).toBe(5);
  await fireEvent(screen.getByRole("slider", { name: "Inspect Gyro" }), new MouseEvent("pointermove", { clientX: 330, bubbles: true }));
  expect(screen.getByText("Time 0.6 s")).toBeVisible();
  expect(screen.getByText("Recorded frame 17")).toBeVisible();
  for (const value of ["10.125 °/s", "11.125 °/s", "12.125 °/s"]) expect(screen.getByText(value)).toBeVisible();
  expect(view.container.querySelector(".cursor")).toHaveAttribute("x1", "332.4");
  await fireEvent.pointerLeave(screen.getByRole("slider"));
  expect(view.container.querySelector(".cursor")).toBeNull();
});
it("supports keyboard inspection, grouped ranges and missing values without interpolation", async () => {
  render(AnalyticsChart, { label: "WB", lines: [{ label: "R", colour: "#ffffff", unit: "neutral", samples: [
    { x: 1, frame: 1, firstFrame: 1, lastFrame: 3, time: 0, value: .5 },
    { x: 4, frame: 4, time: .125, value: null },
  ] }], xMin: 1, xMax: 4, yLabel: "Coefficient" });
  const slider = screen.getByRole("slider", { name: "Inspect WB" });
  slider.focus(); await fireEvent.focus(slider);
  expect(screen.getByText("Frames 1–3")).toBeVisible();
  await fireEvent.keyDown(slider, { key: "End" });
  expect(screen.getByText("Time 0.125 s")).toBeVisible();
  expect(within(slider).getByText(/Unavailable/)).toBeVisible();
  await fireEvent.keyDown(slider, { key: "Escape" });
  expect(screen.queryByText("Time 0.125 s")).toBeNull();
});
it("keeps tiny coefficients, negative data and constant values on finite readable scales", () => {
  for (const [low, high] of [[1e-8, 1.1e-8], [-14, 21], [41.664, 41.664], [0, 0]]) {
    const scale = niceScale(low!, high!);
    expect(scale.low).toBeLessThanOrEqual(low!);
    expect(scale.high).toBeGreaterThanOrEqual(high!);
    expect(scale.ticks.length).toBeGreaterThan(2);
    expect(scale.ticks.every(Number.isFinite)).toBe(true);
    expect(scale.high).toBeGreaterThan(scale.low);
  }
  expect(nearestSample(lines[0]!.samples, .51)).toBe(1);
  expect(chartPath([{ x: 1, value: 1 }, { x: 2, value: null }, { x: 3, value: 2 }], x => x, y => y).match(/M/g)).toHaveLength(2);
});


it("keeps y-axis increments distinct for small changes around a nonzero baseline", () => {
  for (const [low, high] of [[41.66400001, 41.66400009], [1e-8, 1.001e-8]]) {
    const scale = niceScale(low!, high!);
    const labels = scale.ticks.map(value => chartTick(value, scale.step));
    expect(new Set(labels).size).toBe(labels.length);
    expect(labels).not.toContain("Unavailable");
  }
});
