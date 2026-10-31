import { afterEach, expect, it, vi } from "vitest";
import { adjustmentValues, applyAdjustments } from "../src/lib/bulk-adjustments";
import { developApi } from "../src/lib/api";

vi.mock("../src/lib/api", () => ({ api: { adjustmentDefaults: vi.fn(), applyDefaults: vi.fn() }, developApi: { recipe: vi.fn(), save: vi.fn() } }));
afterEach(() => vi.resetAllMocks());

it("copies explicit adjustments while retaining each target's trim, output and other settings", async () => {
  vi.mocked(developApi.recipe).mockResolvedValue({ ok: true, data: { layer: { version: "target-version", values: { trim: { start: "1s" }, output: { kind: "dng" }, develop: { white_balance: "first", exposure: 1 } } }, resolved: {} } });
  vi.mocked(developApi.save).mockResolvedValue({ ok: true, data: { version: "new", values: {} } });
  const result = await applyAdjustments(["a", "b"], { look: "neutral", develop: { rotation: 90 }, trim: { start: "99s" }, output: { kind: "hevc" } });
  expect(result.map(item => item.ok)).toEqual([true, true]);
  expect(developApi.save).toHaveBeenNthCalledWith(1, "a", { version: "target-version", values: { trim: { start: "1s" }, output: { kind: "dng" }, develop: { white_balance: "first", exposure: 1, rotation: 90 } } });
});

it("reports per-clip failures without retrying conflicts or stopping later clips", async () => {
  vi.mocked(developApi.recipe).mockResolvedValue({ ok: true, data: { layer: { version: "v1", values: {} }, resolved: {} } });
  vi.mocked(developApi.save).mockResolvedValueOnce({ ok: false, code: "recipe-changed", message: "Changed elsewhere" }).mockResolvedValueOnce({ ok: true, data: { version: "v2", values: {} } });
  expect(await applyAdjustments(["a", "b"], { develop: { rotation: 90 } })).toEqual([{ id: "a", ok: false, message: "Changed elsewhere" }, { id: "b", ok: true }]);
  expect(developApi.save).toHaveBeenCalledTimes(2);
});

it("applies All through versioned inherited defaults without writing clip layers", async () => {
  const { api } = await import("../src/lib/api");
  const { applyToAll } = await import("../src/lib/bulk-adjustments");
  vi.mocked(api.adjustmentDefaults).mockResolvedValue({ ok: true, data: { version: "defaults-v1", values: {} } });
  vi.mocked(api.applyDefaults).mockResolvedValue({ ok: true, data: { version: "defaults-v2", values: {} } });
  expect((await applyToAll({ develop: { rotation: 90 }, trim: { start: "1s" } })).ok).toBe(true);
  expect(api.applyDefaults).toHaveBeenCalledWith("defaults-v1", { develop: { rotation: 90 } });
  expect(developApi.save).not.toHaveBeenCalled();
});

it("does not copy retired creative settings into Resolve defaults", async () => {
  const { adjustmentValues } = await import("../src/lib/bulk-adjustments");
  expect(adjustmentValues({ look: "gritty", hdr: true, denoise: { luminance: 1 }, grade: { sat: 2 }, develop: { exposure: 2, capture_sharpening: 1, rotation: 90, mount_proxy: "half" } })).toEqual({ develop: { rotation: 90, mount_proxy: "half" } });
});


it("carries explicit source policies without discarding old values in other clips", () => {
  expect(adjustmentValues({ develop: { level_policy: "recorded", camera_model_source: "recorded", exposure: 3 } }))
    .toEqual({ develop: { level_policy: "recorded", camera_model_source: "recorded" } });
});
