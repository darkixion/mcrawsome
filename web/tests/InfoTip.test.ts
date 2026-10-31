import { render, screen, waitFor } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import InfoTip from "../src/components/InfoTip.svelte";

it("shows help on hover and keyboard focus and dismisses it with Escape", async () => {
  render(InfoTip, { label: "Preview GPU", text: "Uses the CPU when a GPU is unavailable." });
  const user = userEvent.setup();
  const button = screen.getByRole("button", { name: "About Preview GPU" });
  expect(screen.queryByRole("tooltip")).toBeNull();
  await user.hover(button);
  expect(screen.getByRole("tooltip")).toHaveTextContent("Uses the CPU");
  await user.unhover(button);
  await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
  await user.tab();
  expect(screen.getByRole("tooltip")).toBeVisible();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("tooltip")).toBeNull();
});

it("keeps the tip outside the button's container, so a moved or contained card cannot shift or clip it", async () => {
  const { container } = render(InfoTip, { label: "Resolve timeline resolution limit", text: "Limited to 3840 x 2160.", warning: true });
  const user = userEvent.setup();
  await user.hover(screen.getByRole("button", { name: "Resolve timeline resolution limit" }));
  const tip = screen.getByRole("tooltip");
  expect(tip.parentElement).toBe(document.body);
  expect(container.contains(tip)).toBe(false);
  await user.hover(tip);  // crossing from the button to the tip keeps it open
  await new Promise(resolve => setTimeout(resolve, 200));
  expect(screen.getByRole("tooltip")).toBe(tip);
  await user.unhover(tip);
  await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
  expect(document.body.contains(tip)).toBe(false);
});
