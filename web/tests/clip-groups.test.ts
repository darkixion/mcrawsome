import { expect, it } from "vitest";
import { recordedTogether, shoots, withGroups } from "../src/lib/clip-groups";
import { facetLabel, facetValue } from "../src/lib/library-filters";
import type { ClipSummary } from "../src/lib/library";

const clip = (id: string, captured: string, duration_s = 20, extra: Partial<ClipSummary> = {}) =>
  ({ id, path: `/clips/${id}.mcraw`, paths: [], name: `${id}.mcraw`, kind: "raw", status: "ready", gps: false, rating: 0, details_text: "", gyro: false, display_rotation: 0, captured, duration_s, ...extra }) as ClipSummary;

it("splits shoots at two-hour gaps and at moves of more than 5 km", () => {
  const clips = [
    clip("a", "2026-10-25T13:44:16Z"), clip("b", "2026-10-25T13:44:57Z"),
    clip("c", "2026-10-25T17:00:00Z"),
    clip("d", "2026-10-25T17:05:00Z", 20, { position: [51.45, -2.59], place: "Bristol, United Kingdom" }),
    clip("e", "2026-10-25T17:10:00Z", 20, { position: [51.38, -2.36] }),  // Bath: 17 km away
    clip("x", null as unknown as string),
  ];
  const labels = shoots(clips);
  expect(labels.get("a")).toBe(labels.get("b"));
  expect(labels.get("c")).toBe(labels.get("d"));
  expect(labels.get("c")).toContain("Bristol, United Kingdom");
  expect(labels.get("d")).not.toBe(labels.get("e"));
  expect(labels.get("b")).not.toBe(labels.get("c"));
  expect(labels.has("x")).toBe(false);
});

it("groups only recordings that overlap in time", () => {
  const clips = [clip("a", "2026-10-25T13:44:00Z", 30), clip("b", "2026-10-25T13:44:20Z", 30), clip("c", "2026-10-25T13:46:00Z", 10)];
  const labels = recordedTogether(clips);
  expect(labels.get("a")).toBe(labels.get("b"));
  expect(labels.get("a")).toMatch(/2 clips$/);
  expect(labels.has("c")).toBe(false);
  const grouped = withGroups(clips);
  expect(facetValue(grouped[2]!, "together")).toBe("None");
  expect(facetLabel("together", "None")).toBe("Not with other clips");
  expect(facetValue(grouped[0]!, "shoot")).toBe(grouped[1]!.shoot);
});

it("names places, and says when a clip has no GPS or no nearby city", () => {
  expect(facetValue(clip("a", "2026-10-25T13:44:00Z", 1, { place: "Bath, United Kingdom", gps: true }), "place")).toBe("Bath, United Kingdom");
  expect(facetValue(clip("b", "2026-10-25T13:44:00Z", 1, { gps: true }), "place")).toBe("No nearby city");
  expect(facetValue(clip("c", "2026-10-25T13:44:00Z"), "place")).toBe("No GPS");
});
