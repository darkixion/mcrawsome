import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import SupportDiagnostics from "../src/components/SupportDiagnostics.svelte";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("collects only on request, exposes the report and reuses native JSON saving", async () => {
  const report = { files: { "server.log": "engine ~/clip.mcraw\n", "doctor.json": "{}" } };
  const fetcher = vi.fn().mockResolvedValue(Response.json(report));
  const save_report = vi.fn().mockResolvedValue(true);
  vi.stubGlobal("fetch", fetcher);
  vi.stubGlobal("pywebview", { api: { save_report } });
  render(SupportDiagnostics);
  expect(fetcher).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole("button", { name: "Collect support report" }));
  await screen.findByText("Review support report");
  expect(JSON.parse((screen.getByRole("textbox", { name: "Support report" }) as HTMLTextAreaElement).value)).toEqual(report);
  expect(fetcher).toHaveBeenCalledWith("/api/v1/support/report", expect.objectContaining({ method: "POST" }));
  await fireEvent.click(screen.getByRole("button", { name: "Save support report JSON" }));
  expect(save_report).toHaveBeenCalledWith(JSON.stringify(report, null, 2) + "\n", "mcrawsome-support.json");
  expect(await screen.findByRole("status")).toHaveTextContent("JSON saved");
});

it("shows collection failure and permits retry", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ error: { code: "client-not-allowed", message: "Local access required" } }, { status: 403 }))
    .mockResolvedValueOnce(Response.json({ files: {} }));
  vi.stubGlobal("fetch", fetcher);
  render(SupportDiagnostics);
  await fireEvent.click(screen.getByRole("button", { name: "Collect support report" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Local access required");
  await fireEvent.click(screen.getByRole("button", { name: "Collect support report" }));
  await screen.findByText("Review support report");
  expect(screen.queryByRole("alert")).toBeNull();
});
