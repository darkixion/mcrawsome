import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import PreviewCache from "../src/components/PreviewCache.svelte";

afterEach(() => vi.unstubAllGlobals());

function engine(active = true, failClear = false) {
  let empty = false;
  const fetch = vi.fn(async (url: string) => {
    if (url.endsWith("/clear")) {
      if (failClear) return new Response(JSON.stringify({ error: { code: "engine-busy", message: "Preview changed; refresh cache usage and try again" } }), { status: 409 });
      empty = true;
    }
    return new Response(JSON.stringify({ active, bytes: active && !empty ? 2 * 1024 ** 2 : 0,
      linear_bytes: active && !empty ? 1024 ** 2 : 0, jpeg_bytes: active && !empty ? 1024 ** 2 : 0,
      limit_bytes: active ? 16 * 1024 ** 2 : 0, entries: active && !empty ? 2 : 0 }), { status: 200 });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

async function open() {
  const details = screen.getByText("Preview cache").closest("details")!;
  details.open = true;
  await fireEvent(details, new Event("toggle"));
}

it("loads on demand and clears only live frames while retaining the open clip's limit", async () => {
  const fetch = engine();
  render(PreviewCache);
  expect(fetch).not.toHaveBeenCalled();
  await open();
  expect(await screen.findByText(/2.0 MiB · 2 cached entries/)).toBeVisible();
  expect(screen.getByText(/Limit for the open clip: 16.0 MiB/)).toBeVisible();
  await fireEvent.click(screen.getByRole("button", { name: "Clear preview frames" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Playback and the current picture are preserved");
  expect(screen.getByText(/0.0 MiB · 0 cached entries/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Clear preview frames" })).toBeDisabled();
  expect(fetch).toHaveBeenCalledWith("/api/v1/cache/preview/clear", expect.objectContaining({ method: "POST" }));
  expect(fetch.mock.calls.every(([url]) => url.startsWith("/api/v1/cache/preview"))).toBe(true);
});

it("reports an inactive preview without offering a clear action", async () => {
  engine(false);
  render(PreviewCache);
  await open();
  expect(await screen.findByText("No active preview.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Clear preview frames" })).toBeDisabled();
});

it("shows a busy or changed-preview error without claiming the cache was cleared", async () => {
  engine(true, true);
  render(PreviewCache);
  await open();
  await waitFor(() => expect(screen.getByRole("button", { name: "Clear preview frames" })).toBeEnabled());
  await fireEvent.click(screen.getByRole("button", { name: "Clear preview frames" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Preview changed; refresh cache usage and try again");
  expect(screen.getByText(/2.0 MiB · 2 cached entries/)).toBeVisible();
  expect(screen.queryByText(/Preview frame cache cleared/)).not.toBeInTheDocument();
});

it("clears unused browser images after worker acknowledgement and reports their estimate separately", async () => {
  engine();
  const clearFrameCache = vi.fn();
  const client = { clearFrameCache, cacheUsage: () => ({ estimated_bytes: 1024 ** 2, limit_bytes: 64 * 1024 ** 2, entries: 1, reclaimable_entries: 0 }) };
  render(PreviewCache, { client });
  await open();
  expect((await screen.findByText(/Browser image estimate/)).closest("p")).toHaveTextContent("1.0 MiB");
  await fireEvent.click(screen.getByRole("button", { name: "Clear preview frames" }));
  await waitFor(() => expect(clearFrameCache).toHaveBeenCalledTimes(1));
  expect(screen.getByText(/Browser image estimate/).closest("p")).toHaveTextContent("1 entries");
});
