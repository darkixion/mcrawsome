import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import CacheManagement from "../src/components/CacheManagement.svelte";

const rows = [
  { kind: "analysis", path: "/cache/analysis", bytes: 123, files: 1, reclaimable: true, limit_bytes: 500 * 1024 ** 2, note: "Reports" },
  { kind: "audio", path: "/cache/audio", bytes: 456, files: 1, reclaimable: false, limit_bytes: null, note: "Playback" },
];
afterEach(() => vi.unstubAllGlobals());
it("shows measured usage on demand and clears only reclaimable entries", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json(rows))
    .mockResolvedValueOnce(Response.json({ ...rows[0], bytes: 0, files: 0 }));
  vi.stubGlobal("fetch", fetcher);
  render(CacheManagement);
  expect(fetcher).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole("button", { name: "Show cache usage" }));
  expect(await screen.findByText("/cache/analysis")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Clear audio" })).toBeNull();
  await fireEvent.click(screen.getByRole("button", { name: "Clear analysis" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Active media and playback are preserved");
  const [url, init] = fetcher.mock.calls[1] as [string, RequestInit];
  expect(url).toBe("/api/v1/cache/clear");
  expect(JSON.parse(init.body as string)).toEqual({ kind: "analysis" });
  expect(screen.getByRole("button", { name: "Clear analysis" })).toBeDisabled();
});
it("keeps usage visible when clearing fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(Response.json(rows))
    .mockResolvedValueOnce(Response.json({ error: { code: "output-unwritable", message: "Read-only cache" } }, { status: 500 })));
  render(CacheManagement);
  await fireEvent.click(screen.getByRole("button", { name: "Show cache usage" }));
  await fireEvent.click(await screen.findByRole("button", { name: "Clear analysis" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Read-only cache");
  expect(screen.getByRole("button", { name: "Clear analysis" })).toBeEnabled();
});
