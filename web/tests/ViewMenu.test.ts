import { fireEvent, render, screen } from "@testing-library/svelte";
import { expect, it } from "vitest";
import Harness from "./ViewMenuHarness.svelte";

it("opens across the whole viewer, so the native picture leaves nothing bare beside it", async () => {
  render(Harness);
  screen.getByTestId("viewer").getBoundingClientRect = () => ({ left: 700, top: 80, right: 1250, bottom: 460, width: 550, height: 380 }) as DOMRect;
  await fireEvent.click(screen.getByRole("button", { name: "Zoom" }));
  const sheet = screen.getByRole("menu", { name: "Zoom" });
  expect(sheet).toHaveClass("sheet");
  expect(sheet).toHaveAttribute("aria-orientation", "horizontal");
  expect(sheet.style.left).toBe("700px");
  expect(sheet.style.width).toBe("550px");
  expect(screen.getByRole("menuitemradio", { name: "Fit" })).toHaveFocus();
  await fireEvent.keyDown(sheet, { key: "ArrowRight" });
  expect(screen.getByRole("menuitemradio", { name: "50%" })).toHaveFocus();
  await fireEvent.keyDown(sheet, { key: "ArrowLeft" });
  expect(screen.getByRole("menuitemradio", { name: "Fit" })).toHaveFocus();
});

it("stays a drop-down list outside a viewer", async () => {
  render(Harness, { viewer: false });
  await fireEvent.click(screen.getByRole("button", { name: "Zoom" }));
  const list = screen.getByRole("menu", { name: "Zoom" });
  expect(list).not.toHaveClass("sheet");
  expect(list).toHaveAttribute("aria-orientation", "vertical");
  expect(list.style.width).toBe("");
  await fireEvent.keyDown(list, { key: "ArrowRight" });
  expect(screen.getByRole("menuitemradio", { name: "Fit" })).toHaveFocus();
});
