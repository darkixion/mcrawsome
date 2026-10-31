import { render, screen } from "@testing-library/svelte";
import { expect, it } from "vitest";
import IconRail from "../src/components/IconRail.svelte";

it("offers the media screens without retired export or queue links", () => {
  render(IconRail, { route: "settings" });
  expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("aria-current", "page");
  expect(screen.getByRole("link", { name: "Library" })).not.toHaveAttribute("aria-current");
  expect(screen.getAllByRole("link").map(link => link.getAttribute("title"))).toEqual(["Library", "Settings", "About"]);
});
