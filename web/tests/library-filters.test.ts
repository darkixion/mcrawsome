import { expect, it } from "vitest";
import { facetLabel, facetValues, matchesFacets } from "../src/lib/library-filters";
import type { ClipSummary } from "../src/lib/library";
const clip = { camera: "Pixel", width: 3840, height: 2160, fps: 23.976, iso: 100, focal_length_mm: 6.9, sensor_bits: 12, captured: "2026-10-20T22:00:00Z", rating: 5 } as ClipSummary;
it("builds sorted facets from cached facts and combines every criterion", () => {
  expect(facetValues([clip], "resolution")).toEqual(["3840 × 2160"]);
  expect(facetValues([clip], "fps")).toEqual(["24"]);
  const filters = { camera: "Pixel", resolution: "3840 × 2160", fps: "24", iso: "100", focal_length_mm: "6.9", sensor_bits: "12", captured: "2026-10-20", rating: "5" };
  expect(matchesFacets(clip, filters)).toBe(true);
  for (const key of Object.keys(filters)) expect(matchesFacets(clip, { ...filters, [key]: "other" })).toBe(false);
  expect(matchesFacets({ ...clip, camera: null }, { camera: "Unknown" })).toBe(true);
  expect(matchesFacets({ ...clip, rating: 0 }, { rating: "0" })).toBe(true);
});

it("rounds frame rates to the whole rate they are near, so 23.79, 23.976 and 24 share one choice", () => {
  const clips = [23.7916, 23.976, 24, 24.0015, 29.97, 59.94].map(fps => ({ ...clip, fps }));
  expect(facetValues(clips, "fps")).toEqual(["24", "30", "60"]);
  expect(clips.filter(item => matchesFacets(item, { fps: "24" })).map(item => item.fps)).toEqual([23.7916, 23.976, 24, 24.0015]);
});

it("offers quick date ranges alongside the existing specific dates", () => {
  const values = facetValues([clip], "captured");
  expect(values).toEqual(["@today", "@this-week", "@this-month", "2026-10-20"]);
  expect(values.map(value => facetLabel("captured", value))).toEqual(["Today", "This week", "This month", "2026-10-20"]);
  expect(facetLabel("captured", "__proto__")).toBe("__proto__");
});

it("matches local calendar days, Monday-start weeks and months without including future clips", () => {
  const now = new Date(2026, 9, 25, 12);
  const captured = (month: number, day: number, hour = 10) => ({ ...clip, captured: new Date(2026, month - 1, day, hour).toISOString() });
  expect(matchesFacets(captured(10, 25), { captured: "@today" }, now)).toBe(true);
  expect(matchesFacets(captured(10, 24), { captured: "@today" }, now)).toBe(false);
  expect(matchesFacets(captured(10, 19), { captured: "@this-week" }, now)).toBe(true);
  expect(matchesFacets(captured(10, 18), { captured: "@this-week" }, now)).toBe(false);
  expect(matchesFacets(captured(10, 1), { captured: "@this-month" }, now)).toBe(true);
  expect(matchesFacets(captured(9, 30), { captured: "@this-month" }, now)).toBe(false);
  for (const range of ["@today", "@this-week", "@this-month"]) {
    expect(matchesFacets(captured(10, 25, 13), { captured: range }, now)).toBe(false);
    expect(matchesFacets(captured(10, 26), { captured: range }, now)).toBe(false);
    expect(matchesFacets({ ...clip, captured: null }, { captured: range }, now)).toBe(false);
    expect(matchesFacets({ ...clip, captured: "invalid" }, { captured: range }, now)).toBe(false);
  }
  expect(matchesFacets(captured(10, 20), { captured: "@this-week", camera: "Other" }, now)).toBe(false);
});

it("handles calendar boundaries across midnight and the new year", () => {
  const now = new Date(2027, 0, 1, 0, 5);
  const previousYear = { ...clip, captured: new Date(2026, 11, 31, 23, 59).toISOString() };
  expect(matchesFacets(previousYear, { captured: "@this-week" }, now)).toBe(true);
  expect(matchesFacets(previousYear, { captured: "@this-month" }, now)).toBe(false);
  expect(matchesFacets(previousYear, { captured: "@today" }, now)).toBe(false);
  const monday = new Date(2027, 0, 4, 0, 5);
  expect(matchesFacets(previousYear, { captured: "@this-week" }, monday)).toBe(false);
});


it("uses calendar boundaries across clock changes rather than fixed 24-hour offsets", () => {
  for (const [month, day] of [[2, 29], [9, 25]] as const) {
    const now = new Date(2026, month, day, 23, 59);
    const early = { ...clip, captured: new Date(2026, month, day, 0, 1).toISOString() };
    const previous = { ...clip, captured: new Date(2026, month, day - 1, 23, 59).toISOString() };
    expect(matchesFacets(early, { captured: "@today" }, now)).toBe(true);
    expect(matchesFacets(previous, { captured: "@today" }, now)).toBe(false);
  }
});

it("filters by the movement measured from the gyro log, in plain words", () => {
  const panning = { ...clip, movement: "pan" } as ClipSummary;
  expect(facetValues([panning, clip], "movement")).toEqual(["pan", "Unknown"]);
  expect(facetLabel("movement", "pan")).toBe("Pan or tilt");
  expect(facetLabel("movement", "moving")).toBe("Moving");
  expect(matchesFacets(panning, { movement: "pan" })).toBe(true);
  expect(matchesFacets(clip, { movement: "pan" })).toBe(false);
});
