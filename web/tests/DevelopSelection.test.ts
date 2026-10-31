import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import DevelopSelection from "../src/components/DevelopSelection.svelte";
import { clips, type ClipSummary } from "../src/lib/library";
import { selection } from "../src/lib/selection";
import { applyAdjustments } from "../src/lib/bulk-adjustments";

vi.mock("../src/lib/bulk-adjustments", async importOriginal => ({ ...await importOriginal<typeof import("../src/lib/bulk-adjustments")>(), applyAdjustments: vi.fn() }));
afterEach(() => { clips.set(null); selection.set([]); vi.resetAllMocks(); });

it("keeps per-clip results across a same-value save and retries only failed targets", async () => {
  clips.set(["a", "b", "c"].map(id => ({ id, name: `${id}.mcraw`, status: "ready" })) as ClipSummary[]);
  selection.set(["a", "b", "c"]);
  vi.mocked(applyAdjustments).mockImplementationOnce(async (_ids, _values, progress) => {
    const results = [{ id: "b", ok: false, message: "Changed elsewhere" }, { id: "c", ok: true }];
    results.forEach(result => progress?.(result)); return results;
  }).mockImplementationOnce(async (_ids, _values, progress) => { const results = [{ id: "b", ok: true }]; results.forEach(result => progress?.(result)); return results; });
  const view = render(DevelopSelection, { id: "a", values: { develop: { rotation: 90 } } });
  await fireEvent.click(screen.getByRole("button", { name: "Apply to Selected (3 clips)" }));
  await screen.findByText("b.mcraw: Changed elsewhere");
  await view.rerender({ id: "a", values: { develop: { rotation: 90 } } });
  await fireEvent.click(await screen.findByRole("button", { name: "Retry failed clips" }));
  expect(vi.mocked(applyAdjustments).mock.calls[1]?.[0]).toEqual(["b"]);
  await waitFor(() => expect(screen.getByText(/^2 applied/)).toHaveAttribute("role", "status"));
});

it("counts every selected clip, lists none of them and explains both buttons", () => {
  clips.set(["a", "b", "c"].map(id => ({ id, name: `${id}.mcraw`, status: "ready" })) as ClipSummary[]);
  selection.set(["a", "b", "c"]);
  render(DevelopSelection, { id: "a", values: { motion: { crop: 1.2 } } });
  expect(screen.getByRole("button", { name: "Apply to Selected (3 clips)" })).toBeEnabled();
  expect(screen.queryByRole("link")).toBeNull();
  expect(screen.queryByRole("button", { name: "Apply to All" })).toBeNull();
  expect(screen.getByRole("button", { name: "Set as Default" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "About Apply to Selected" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "About Set as Default" })).toBeInTheDocument();
});

it("offers Set as Default once this clip has settings of its own, and Reset", async () => {
  clips.set([{ id: "a", name: "a.mcraw", status: "ready" }] as ClipSummary[]);
  selection.set(["a"]);
  const onreset = vi.fn();
  const view = render(DevelopSelection, { id: "a", values: {}, onreset });
  // Nothing of this clip's own to make the default yet.
  expect(screen.getByRole("button", { name: "Set as Default" })).toBeDisabled();
  await fireEvent.click(screen.getByRole("button", { name: "Reset" }));
  expect(onreset).toHaveBeenCalledOnce();
  await view.rerender({ id: "a", values: { motion: { crop: 1.2 } }, onreset });
  expect(screen.getByRole("button", { name: "Set as Default" })).toBeEnabled();
});

it("offers this clip's Suggest trim, or Suggest trims for several selected clips with gyro logs", async () => {
  clips.set(["a", "b"].map(id => ({ id, name: `${id}.mcraw`, status: "ready", gyro: true })) as ClipSummary[]);
  selection.set(["a"]);
  const onsuggesttrim = vi.fn();
  const view = render(DevelopSelection, { id: "a", values: {}, onsuggesttrim, trimNote: "In moved past the press of record" });
  await fireEvent.click(screen.getByRole("button", { name: "Suggest trim" }));
  expect(onsuggesttrim).toHaveBeenCalledOnce();
  expect(screen.getByText("In moved past the press of record")).toHaveAttribute("role", "status");
  selection.set(["a", "b"]);
  await view.rerender({ id: "a", values: {}, onsuggesttrim });
  expect(screen.queryByRole("button", { name: "Suggest trim" })).toBeNull();
  expect(screen.getByRole("button", { name: "Suggest trims (2 clips)" })).toBeInTheDocument();
});
