import { fireEvent, render, screen } from "@testing-library/svelte";
import { beforeEach, expect, it, vi } from "vitest";
import DevelopPanel from "../src/components/DevelopPanel.svelte";
import { openSections } from "../src/lib/sections";

beforeEach(() => openSections.set({ source: true, stabilise: true, resolve: true, review: true, "raw-media": true, "raw-advanced": true }));

it("gives the RAW controls where each inherited setting comes from, and their changes of several settings at once", async () => {
  const oncolour = vi.fn(), onchange = vi.fn();
  render(DevelopPanel, {
    recipe: { develop: { black_level: 42, white_level: 3000 } }, values: {},
    sources: { black_level: "Folder clips", white_level: "Application defaults" },
    raw: { blacks: [64, 64, 64, 64], white: 4095 }, problem: null, onchange, onclear: vi.fn(), oncolour,
  });
  expect(screen.getByText("Folder clips")).toBeInTheDocument();
  expect(screen.getByText("Application defaults")).toBeInTheDocument();
  await fireEvent.change(screen.getByRole("combobox", { name: "White / Black Levels" }), { target: { value: "16383/1024" } });
  expect(oncolour).toHaveBeenLastCalledWith({ level_policy: "explicit", white_level: 16383, black_level: 1024 });
  expect(onchange).not.toHaveBeenCalled(); // one change of three settings, not three of one
});
