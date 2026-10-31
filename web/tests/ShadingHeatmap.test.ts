import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import ShadingHeatmap from "../src/components/ShadingHeatmap.svelte";

afterEach(() => vi.unstubAllGlobals());

it("switches between channel gains and the flat field of the same map", async () => {
  const map = { frame: 0, width: 2, height: 1, channels: [[1, 2], [1, 2], [1, 2], [1, 2]] };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(map)));
  render(ShadingHeatmap, { id: "a", frames: [0], step: 30 });
  expect(await screen.findByRole("img", { name: "R lens shading map at frame 1" })).toBeInTheDocument();
  await fireEvent.change(screen.getByRole("combobox", { name: "Shading view" }), { target: { value: "flat" } });
  expect(screen.getByLabelText("Flat field of the lens shading at frame 1").tagName).toBe("CANVAS");
  expect(screen.queryByRole("combobox", { name: "Shading channel" })).toBeNull();
  expect(fetch).toHaveBeenCalledOnce();
});
