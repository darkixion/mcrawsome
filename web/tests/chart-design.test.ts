import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, expect, it } from "vitest";
import { channelColour, HEATMAP_STOPS, heatmapColour } from "../src/lib/analytics-catalogue";
import { chartEnvelope, chartOutliers } from "../src/lib/analytics-chart";
import { overlayModel, overlayEnvelope } from "../src/lib/preview-overlay";
import AnalyticsChart from "../src/components/AnalyticsChart.svelte";

afterEach(cleanup);
it("keeps sensor channels recognisable across capture charts and preview overlays", () => {
  const labels = ["R", "Gr", "Gb", "B", "X", "Y", "Z"];
  for (const label of labels) {
    expect(channelColour(`${label} mean gain`, 3)).toBe(channelColour(label, 0));
    expect(overlayModel({ key: "shading", label: "Lens shading", unit: "gain", help: "", series: [{ label, points: [{ frame: 0, value: 1 }] }] }, 1).lines[0]!.colour).toBe(channelColour(label));
  }
  expect(new Set(labels.slice(0, 4).map(label => channelColour(label))).size).toBe(4);
  expect(channelColour("G (R row)")).toBe(channelColour("Gr"));
  expect(channelColour("G (B row)")).toBe(channelColour("Gb"));
});
it("closes range bands at gaps without inventing bounds or bridging missing means", () => {
  const samples = [
    { x: 0, value: 2, minimum: 1, maximum: 3 }, { x: 1, value: 3, minimum: 2, maximum: 4 },
    { x: 2, value: null, minimum: 0, maximum: 9 },
    { x: 3, value: 5, minimum: 4, maximum: 6 }, { x: 4, value: 6, minimum: 5, maximum: 7 },
  ];
  const band = chartEnvelope(samples, v => v, v => v);
  expect(band).toBe("M0.00,3.00 L1.00,4.00 L1.00,2.00 L0.00,1.00 Z M3.00,6.00 L4.00,7.00 L4.00,5.00 L3.00,4.00 Z");
  expect(chartEnvelope([{ x: 0, value: 1 }], v => v, v => v)).toBe("");
  expect(overlayEnvelope(samples.map(p => ({ x: p.x / 4, y: p.value, low: p.minimum, high: p.maximum }))).match(/M/g)).toHaveLength(2);
});
it("marks actual grouped timing extrema using the existing missing/short interval thresholds", () => {
  const samples = [{ x: 1, value: 40, minimum: 39, maximum: 41 }, { x: 2, value: 45, minimum: 0, maximum: 90 }, { x: 3, value: 20, minimum: 15, maximum: 60 }, { x: 4, value: null, minimum: 0, maximum: 99 }];
  expect(chartOutliers(samples, 40)).toEqual([{ x: 2, value: 90, kind: "Long interval" }, { x: 2, value: 0, kind: "Non-positive interval" }, { x: 3, value: 15, kind: "Short interval" }]);
  expect(chartOutliers(samples, 0)).toEqual([]);
});
it("shows labelled legends, exact extrema and named timing markers", () => {
  const lines = [{ label: "Interval", colour: channelColour("Interval"), unit: "ms", samples: [{ x: 1, value: 40, minimum: 20, maximum: 90 }] }];
  const view = render(AnalyticsChart, { label: "Timing", lines, xMin: 1, xMax: 2, yLabel: "ms", reference: 40, highlightIntervals: true });
  expect(screen.getByRole("list", { name: "Timing legend" })).toHaveTextContent("Interval");
  expect(screen.getByText("20–90")).toBeVisible();
  expect(view.container.querySelector(".outlier title")).toHaveTextContent("Long interval: 90 ms");
});
it("uses a clamped sequential gain scale, including constant maps", () => {
  expect(heatmapColour(1, 1, 3)).toBe(HEATMAP_STOPS[0]);
  expect(heatmapColour(3, 1, 3)).toBe(HEATMAP_STOPS.at(-1));
  expect(heatmapColour(-1, 1, 3)).toBe(HEATMAP_STOPS[0]);
  expect(heatmapColour(9, 1, 3)).toBe(HEATMAP_STOPS.at(-1));
  expect(heatmapColour(1, 1, 1)).toBe(HEATMAP_STOPS[2]);
});
it("retains isolated measurements on either side of missing samples", () => {
  const lines = [{ label: "R", colour: channelColour("R"), unit: "gain", samples: [{ x: 1, value: 1 }, { x: 2, value: null }, { x: 3, value: 2 }] }];
  const view = render(AnalyticsChart, { label: "Sparse", lines, xMin: 1, xMax: 3, yLabel: "gain" });
  expect(view.container.querySelectorAll(".isolated")).toHaveLength(2);
});
it("keeps the remaining axis line style when an earlier axis is hidden", () => {
  const lines = ["X", "Y", "Z"].map((label, index) => ({ label, colour: channelColour(label), unit: "°/s", visible: index !== 0, samples: [{ x: 0, value: 0 }, { x: 1, value: 1 }] }));
  const view = render(AnalyticsChart, { label: "Axes", lines, xMin: 0, xMax: 1, yLabel: "°/s" });
  expect(view.container.querySelector(".trace")).toHaveAttribute("stroke-dasharray", "8 3");
  expect(screen.getByRole("list", { name: "Axes legend" }).querySelectorAll("li")).toHaveLength(2);
});
