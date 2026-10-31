import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import ResetControls from "../src/components/ResetControls.svelte";

afterEach(() => vi.unstubAllGlobals());
const option = { scope: "defaults", version: "before", description: "Reset application adjustment defaults." };
const backup = { scope: "defaults", version: "after", backup: "a".repeat(32), backup_path: "/state/reset-backups/a.json" };
it("loads reset scope, saves its backup and restores without overwriting newer versions", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json([option])).mockResolvedValueOnce(Response.json([]))
    .mockResolvedValueOnce(Response.json(backup)).mockResolvedValueOnce(Response.json({ ...backup, version: "before" }))
    .mockResolvedValueOnce(Response.json([option])).mockResolvedValueOnce(Response.json([]));
  vi.stubGlobal("fetch", fetcher);
  render(ResetControls);
  await fireEvent.click(screen.getByRole("button", { name: "Show reset options" }));
  await fireEvent.click(await screen.findByRole("button", { name: "Reset defaults" }));
  expect(await screen.findByText("/state/reset-backups/a.json")).toBeVisible();
  await fireEvent.click(screen.getByRole("button", { name: "Restore previous defaults" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Previous settings restored");
  const [url, init] = fetcher.mock.calls[3] as [string, RequestInit];
  expect(url).toBe("/api/v1/reset/restore");
  expect(JSON.parse(init.body as string)).toEqual({ backup: backup.backup, version: "after" });
});
it("recovers a persisted backup after reopening the controls", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(Response.json([option])).mockResolvedValueOnce(Response.json([backup])));
  render(ResetControls);
  await fireEvent.click(screen.getByRole("button", { name: "Show reset options" }));
  expect(await screen.findByRole("button", { name: "Restore previous defaults" })).toBeEnabled();
});
