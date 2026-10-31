import { fireEvent, render, screen } from "@testing-library/svelte";
import { createRawSnippet } from "svelte";
import { get } from "svelte/store";
import { expect, it } from "vitest";
import SectionCard from "../src/components/SectionCard.svelte";
import { openSections } from "../src/lib/sections";

const children = createRawSnippet(() => ({ render: () => "<p>Mode and crop</p>" }));
const control = createRawSnippet(() => ({ render: () => '<input type="checkbox" role="switch" aria-label="Stabilise" />' }));

it("folds to its title and summary, keeps its switch within reach and remembers being opened", async () => {
  render(SectionCard, { id: "stabilise", title: "Stabilise", summary: "Smooth · crop 1.10", children, control });
  const fold = screen.getByRole("button", { name: /^Stabilise/ });
  expect(fold).toHaveAttribute("aria-expanded", "false");
  expect(fold).toHaveTextContent("Smooth · crop 1.10");
  expect(screen.queryByText("Mode and crop")).toBeNull();
  // The switch is not part of the fold button, so it works while folded.
  expect(fold).not.toContainElement(screen.getByRole("switch", { name: "Stabilise" }));
  await fireEvent.click(fold);
  expect(fold).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("Mode and crop")).toBeVisible();
  expect(screen.getByRole("region", { name: "Stabilise" })).toContainElement(screen.getByText("Mode and crop"));
  expect(get(openSections)).toEqual({ stabilise: true });
  expect(JSON.parse(localStorage.getItem("mcrawsome.open-sections") ?? "{}")).toEqual({ stabilise: true });
});

it("starts open when asked, until folded", async () => {
  render(SectionCard, { id: "clip-info", title: "Info", initiallyOpen: true, children });
  const fold = screen.getByRole("button", { name: "Info" });
  expect(fold).toHaveAttribute("aria-expanded", "true");
  await fireEvent.click(fold);
  expect(screen.queryByText("Mode and crop")).toBeNull();
  expect(get(openSections)).toEqual({ "clip-info": false });
});
