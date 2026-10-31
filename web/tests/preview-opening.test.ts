import { afterEach, expect, it, vi } from "vitest";
import { previewRecipe, warmPreview, takePreview, discardWarmPreview, hoverPreview, leavePreview } from "../src/lib/preview-opening";
import { developApi } from "../src/lib/api";

const closed = vi.fn();
const opened = vi.fn();
vi.mock("../src/lib/preview", () => ({ PreviewClient: class { constructor(id: string) { opened(id); } close = closed; } }));
vi.mock("../src/lib/api", () => ({ developApi: { recipe: vi.fn(async () => ({ ok: true, data: {} })) } }));

afterEach(() => { discardWarmPreview(); vi.useRealTimers(); closed.mockClear(); opened.mockClear(); vi.mocked(developApi.recipe).mockClear(); });
it("keeps saved video source colour and camera conversion in the neutral preview", () => {
  const source = { input_encoding: "auto", input_gamut: "camera-native", input_transfer: "s-log3", input_ei: 800, native_as: "ap1" };
  const camera = { colour_matrix: [1,0,0,0,1,0,0,0,1] };
  expect(previewRecipe({ develop: { exposure: 2, ...source }, camera })).toMatchObject({ develop: { exposure: 0, ...source }, camera, motion: { stabilise: "off" } });
});
it("reuses one preopened session and recipe request when Develop takes the clip", () => {
  const first = warmPreview("a");
  expect(warmPreview("a")).toBe(first);
  expect(takePreview("a")).toBe(first);
  expect(closed).not.toHaveBeenCalled();
});
it("closes the previous speculative session when another clip is chosen", () => {
  warmPreview("a"); warmPreview("b");
  expect(closed).toHaveBeenCalledOnce();
});
it("expires an unused session without closing one already taken by Develop", () => {
  vi.useFakeTimers();
  warmPreview("a");
  vi.advanceTimersByTime(10_000);
  expect(closed).toHaveBeenCalledOnce();
  takePreview("b");
  vi.advanceTimersByTime(10_000);
  expect(closed).toHaveBeenCalledOnce();
});
it("waits for hover intent so crossing cards does not queue preview processes", () => {
  vi.useFakeTimers();
  hoverPreview("a");
  vi.advanceTimersByTime(50); leavePreview("a");
  hoverPreview("b");
  vi.advanceTimersByTime(50); leavePreview("b");
  vi.advanceTimersByTime(200);
  expect(opened).not.toHaveBeenCalled();
  hoverPreview("c");
  vi.advanceTimersByTime(150);
  expect(opened).toHaveBeenCalledExactlyOnceWith("c");
});

it("keeps the neutral preview's saved sensor correction", () => {
  const profile = { version: 1, sensor_width: 128, sensor_height: 64, cfa: "rggb", map_width: 2, map_height: 2, gains: [[2,2,2,2],[2,2,2,2],[2,2,2,2],[2,2,2,2]] };
  expect(previewRecipe({ develop: { exposure: 2, vignette_profile: profile } })).toMatchObject({ develop: { exposure: 0, vignette_profile: profile }, motion: { stabilise: "off" } });
});

it("carries level and shading calibration into the neutral preview", () => {
  const calibration = { rotation: 90, level_policy: "recorded", black_level: [10, 20, 30, 40], white_level: 3000, lens_shading: 0.5, lens_shading_mode: "colour" };
  expect(previewRecipe({ look: "bond", develop: { exposure: 2, ...calibration } }).develop).toEqual({ exposure: 0, ...calibration });
});

it("asks for a clip's saved settings once, however often it is warmed before Develop takes it", async () => {
  warmPreview("a"); warmPreview("a");
  const develop = takePreview("a");
  expect(await develop.recipe).toEqual({ ok: true, data: {} });
  expect(developApi.recipe).toHaveBeenCalledExactlyOnceWith("a");
  expect(opened).toHaveBeenCalledTimes(1);
});

it("uses the mounted crop for preview framing without changing the saved settings", () => {
  const saved = { develop: { mount_crop: "1024x1024", rotation: 90 } };
  expect(previewRecipe(saved).develop).toMatchObject({ crop: "1024x1024", rotation: 90 });
  expect(saved.develop).toEqual({ mount_crop: "1024x1024", rotation: 90 });
});
