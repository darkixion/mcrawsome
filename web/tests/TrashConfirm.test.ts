import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import TrashConfirm from "../src/components/TrashConfirm.svelte";
import type { ClipSummary } from "../src/lib/library";

afterEach(() => vi.unstubAllGlobals());

const clip = { id: "a", name: "a.0.mcraw", path: "/clips/a.0.mcraw", paths: ["/clips/a.0.mcraw", "/backup/a.0.mcraw"], kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;

it("lists every copy, then reports what moved and the split parts kept", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ trashed: ["/clips/a.0.mcraw", "/clips/a.0.gcsv", "/backup/a.0.mcraw"], unmounted: ["m"], kept_parts: ["/clips/a.1.mcraw"] })));
  const onchanged = vi.fn();
  render(TrashConfirm, { chosen: [clip], onclose: vi.fn(), onchanged });
  expect(screen.getByText("/backup/a.0.mcraw")).toBeInTheDocument();
  await fireEvent.click(screen.getByRole("button", { name: "Move to Trash" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Moved 3 files to the Trash after unmounting 1.");
  expect(screen.getByText("Another part of the split recording stays where it is:")).toBeInTheDocument();
  expect(screen.getByText("/clips/a.1.mcraw")).toBeInTheDocument();
  expect(onchanged).toHaveBeenCalledOnce();
  const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/api/v1/clips/trash");
  expect(JSON.parse(String(init.body))).toEqual({ clip_ids: ["a"] });
});

it("keeps the choice open when the engine refuses", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ error: { code: "engine-busy", message: "An editor has the mounted media of a.0.mcraw open; close it there first" } }, { status: 409 })));
  const onchanged = vi.fn();
  render(TrashConfirm, { chosen: [clip], onclose: vi.fn(), onchanged });
  await fireEvent.click(screen.getByRole("button", { name: "Move to Trash" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("close it there first");
  expect(screen.getByRole("button", { name: "Move to Trash" })).toBeEnabled();
  expect(onchanged).not.toHaveBeenCalled();
});
