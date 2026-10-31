import { expect, it } from "vitest";
import { parseProfile } from "../src/lib/vignette";
import { gainMaps, noiseCurves } from "../src/lib/calibration-visuals";

const profile = { version: 1, sensor_width: 128, sensor_height: 64, cfa: "rggb", map_width: 2, map_height: 2, gains: Array.from({ length: 4 }, () => [1,2,3,4]) };
it("refuses malformed, unbounded and non-finite profile data before applying", () => {
  expect(parseProfile(profile)).toEqual(profile);
  for (const change of [{ version: 2 }, { map_width: 129 }, { gains: [[1,2,3,4]] }, { gains: Array.from({ length: 4 }, () => [0,1,2,3]) }, { sensor_width: 0 }, { unknown: true }]) expect(() => parseProfile({ ...profile, ...change })).toThrow();
  expect(() => parseProfile({ ...profile, gains: Array.from({ length: 4 }, () => [NaN, Infinity, 2,3]) })).toThrow();
});
it("bounds visual cells while preserving the shared full-map extrema", () => {
  const plane = Array.from({ length: 128 * 128 }, (_, i) => i === 9000 ? 8 : 1);
  const maps = gainMaps({ lensShadingMapWidth: 128, lensShadingMapHeight: 128, lensShadingMap: [plane,plane,plane,plane] });
  expect(maps?.planes[0]).toHaveLength(24 * 24);
  expect(maps?.max).toBe(8); expect(maps?.min).toBe(1);
  expect(maps?.sampled).toBe(true);
  expect(gainMaps({ lensShadingMapWidth: 100000, lensShadingMapHeight: 2, lensShadingMap: [] })).toBeNull();
  expect(noiseCurves({ noiseProfile: [1,0,1,0,1,0,1,0] })?.max).toBe(100);
  expect(noiseCurves({ noiseProfile: [NaN] })).toBeNull();
});
