import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../src/lib/api";

function reply(status: number, body: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api", () => {
  it("gives the data of a successful call", async () => {
    reply(200, { checks: [] });
    expect(await api.doctor()).toEqual({ ok: true, data: { checks: [] } });
  });

  it("turns the engine's 422 error body into a result naming the field", async () => {
    const message = "folders: /gone does not exist or is not a folder";
    reply(422, { error: { code: "recipe-invalid", message, field: "folders" } });
    const result = await api.saveSettings({ folders: ["/gone"], show_preview_stats: false, preview_gpu: true, quad_bayer_remosaic: true, preview_cache_mib: 256, resolve_full_resolution: false, resolve_separate_audio: false, resolve_link_audio: true, resolve_keywords: false, timecode: "zero", timecode_zone: "computer", keep_engine: false, remount_on_start: false, resolve_route: "auto" });
    expect(result).toEqual({ ok: false, code: "recipe-invalid", message, field: "folders" });
  });

  it("turns FastAPI's own validation body into the same shape", async () => {
    reply(422, { detail: [{ loc: ["body", "workers"], msg: "Input should be greater than or equal to 1", type: "x" }] });
    const result = await api.saveSettings({ workers: 0, show_preview_stats: false, preview_gpu: true, quad_bayer_remosaic: true, preview_cache_mib: 256, resolve_full_resolution: false, resolve_separate_audio: false, resolve_link_audio: true, resolve_keywords: false, timecode: "zero", timecode_zone: "computer", keep_engine: false, remount_on_start: false, resolve_route: "auto" });
    expect(result).toEqual({
      ok: false,
      code: "recipe-invalid",
      message: "workers: Input should be greater than or equal to 1",
      field: "workers",
    });
  });

  it("gives an error without a field when the engine names none", async () => {
    reply(403, { error: { code: "path-not-allowed", message: "/x is outside the library folders" } });
    expect(await api.health()).toEqual({
      ok: false,
      code: "path-not-allowed",
      message: "/x is outside the library folders",
    });
  });

  it("says so when the engine does not answer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const result = await api.health();
    expect(result.ok).toBe(false);
    expect(result.ok === false && result.code).toBe("engine-unreachable");
  });

  it("sends the body as JSON", async () => {
    reply(200, { folders: [], show_preview_stats: false, preview_gpu: true, quad_bayer_remosaic: true, preview_cache_mib: 256, resolve_full_resolution: false, resolve_separate_audio: false, resolve_link_audio: true, resolve_keywords: false, timecode: "zero", timecode_zone: "computer", keep_engine: true, remount_on_start: false, resolve_route: "off" });
    await api.saveSettings({ folders: [], show_preview_stats: false, preview_gpu: true, quad_bayer_remosaic: true, preview_cache_mib: 256, resolve_full_resolution: false, resolve_separate_audio: false, resolve_link_audio: true, resolve_keywords: false, timecode: "zero", timecode_zone: "computer", keep_engine: true, remount_on_start: false, resolve_route: "off" });
    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/settings");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({ folders: [], show_preview_stats: false, preview_gpu: true, quad_bayer_remosaic: true, preview_cache_mib: 256, resolve_full_resolution: false, resolve_separate_audio: false, resolve_link_audio: true, resolve_keywords: false, timecode: "zero", timecode_zone: "computer", keep_engine: true, remount_on_start: false, resolve_route: "off" });
  });
});
