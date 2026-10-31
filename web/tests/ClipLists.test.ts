import { render, screen } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import ClipLists from "../src/components/ClipLists.svelte";

afterEach(() => vi.unstubAllGlobals());

it("asks the engine for a camera report of the selected clips and saves it", async () => {
  const requests: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    requests.push(JSON.parse(String(init?.body)));
    return Response.json({ name: "camera-report.csv", content: "Name\na.mcraw\n" });
  }));
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:report"), revokeObjectURL: vi.fn() });
  render(ClipLists, { ids: ["a", "b"], positions: 0 });
  const user = userEvent.setup();
  await user.click(screen.getByText("Lists"));
  expect(screen.getByRole("button", { name: "Locations (GPX)" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Camera report (CSV)" }));
  expect(requests).toEqual([{ ids: ["a", "b"], format: "csv" }]);
  expect(await screen.findByRole("status")).toHaveTextContent("camera-report.csv downloaded");
});
