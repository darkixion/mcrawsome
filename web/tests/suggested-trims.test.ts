import { afterEach, expect, it, vi } from "vitest";
import { api, developApi } from "../src/lib/api";
import { suggestTrims, trimSummary, trimValues } from "../src/lib/suggested-trims";

vi.mock("../src/lib/api", () => ({ api: { motion: vi.fn() }, developApi: { recipe: vi.fn(), save: vi.fn() } }));
afterEach(() => vi.resetAllMocks());

const jolted = { start_s: 0.5, end_s: 18.25, reasons: ["Start: a handling jolt (up to 64 degrees/s) until 0.5 s", "End: a handling jolt (up to 76 degrees/s) from 18.2 s, usually pressing stop"] };
const report = (trim: object | null, notes: string[] = []) => ({ ok: true as const, data: { trim, notes, shares: {}, blurred: [] } });

it("turns a suggestion into recipe trim times and plain reasons", () => {
  expect(trimValues(jolted)).toEqual({ start: "0.5s", duration: "17.75s" });
  expect(trimSummary(jolted)).toBe(`${jolted.reasons.join(". ")}.`);
  expect(trimSummary({ start_s: 0, end_s: 10, reasons: [] })).toBe("No handling jolt or settling focus was found; the marks are unchanged.");
  expect(trimSummary(null, ["Movement, blur and trim suggestions need the clip's gyro log"])).toBe("Movement, blur and trim suggestions need the clip's gyro log");
});

it("saves suggested marks for a selection but keeps marks clips already have", async () => {
  vi.mocked(api.motion).mockResolvedValue(report(jolted) as never);
  vi.mocked(developApi.recipe)
    .mockResolvedValueOnce({ ok: true, data: { layer: { version: "v1", values: { motion: { stabilise: "on" } } }, resolved: {} } })
    .mockResolvedValueOnce({ ok: true, data: { layer: { version: "v2", values: { trim: { start: "2s" } } }, resolved: {} } });
  vi.mocked(developApi.save).mockResolvedValue({ ok: true, data: { version: "v3", values: {} } });
  const results = await suggestTrims(["a", "b"]);
  expect(results.map(item => [item.id, item.ok, item.changed])).toEqual([["a", true, true], ["b", true, false]]);
  expect(results[1]!.message).toBe("Kept the marks this clip already has.");
  expect(developApi.save).toHaveBeenCalledOnce();
  expect(developApi.save).toHaveBeenCalledWith("a", { version: "v1", values: { motion: { stabilise: "on" }, trim: { start: "0.5s", duration: "17.75s" } } });
});

it("leaves clips unchanged when nothing needs trimming or the report fails", async () => {
  vi.mocked(api.motion)
    .mockResolvedValueOnce(report({ start_s: 0, end_s: 10, reasons: [] }) as never)
    .mockResolvedValueOnce({ ok: false, code: "input-unreadable", message: "Cannot measure b.mcraw" } as never);
  const results = await suggestTrims(["a", "b"]);
  expect(results).toEqual([
    { id: "a", ok: true, changed: false, message: "No handling jolt or settling focus was found; the marks are unchanged." },
    { id: "b", ok: false, changed: false, message: "Cannot measure b.mcraw" },
  ]);
  expect(developApi.recipe).not.toHaveBeenCalled();
});
