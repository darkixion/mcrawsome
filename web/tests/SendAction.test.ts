import { get } from "svelte/store";
import { fireEvent, render, screen } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import SendAction from "../src/components/SendAction.svelte";
import { preferredTarget, sendActivity, type SendTarget } from "../src/lib/send";

beforeEach(() => { preferredTarget.set("resolve"); sendActivity.set(null); });
const targets: SendTarget[] = ["Resolve", "Second editor"].map(label => ({
  id: label === "Resolve" ? "resolve" : "test-editor", label,
  send: vi.fn(), disabled: () => null,
}));
it("keeps one direct action while only one destination is available", () => {
  render(SendAction, { targets: [targets[0]!], onclick: vi.fn() });
  expect(screen.getByRole("button", { name: "Send to Resolve" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Choose send option" })).toBeNull();
});
it("adds one keyboard-accessible destination chooser when another adapter is available", async () => {
  const choose = vi.fn();
  render(SendAction, { targets, onclick: choose });
  const user = userEvent.setup();
  const trigger = screen.getByRole("button", { name: "Choose send option" });
  trigger.focus();
  await user.keyboard("{ArrowDown}");
  expect(await screen.findByRole("menuitem", { name: "Resolve" })).toHaveFocus();
  await user.keyboard("{ArrowDown}{Enter}");
  expect(choose).toHaveBeenCalledWith(targets[1]);
  expect(screen.queryByRole("menu")).toBeNull();
  expect(screen.getByRole("button", { name: "Send to Second editor" })).toBeInTheDocument();
  await user.click(trigger);
  await fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
  expect(screen.queryByRole("menu")).toBeNull();
  expect(screen.getByRole("button", { name: "Send to Second editor" })).toHaveFocus();
});

it("names media bin and timeline destinations in the shared chooser through the shared chooser", async () => {
  const choose = vi.fn();
  render(SendAction, { onclick: choose });
  expect(screen.getByRole("button", { name: "Send to Resolve (Media Bin)" })).toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Choose send option" }));
  await user.click(screen.getByRole("menuitem", { name: "Resolve (Timeline)" }));
  expect(choose).toHaveBeenCalledWith(expect.objectContaining({ id: "resolve", mode: "timeline" }));
  expect(screen.getByRole("button", { name: "Send to Resolve (Timeline)" })).toBeInTheDocument();
  expect(screen.queryByRole("menu")).toBeNull();
});

it("remembers the timeline choice", async () => {
  const choose = vi.fn();
  render(SendAction, { onclick: choose });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Choose send option" }));
  expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["Resolve (Media Bin)", "Resolve (Timeline)"]);
  await user.click(screen.getByRole("menuitem", { name: "Resolve (Timeline)" }));
  expect(choose).toHaveBeenCalledWith(expect.objectContaining({ id: "resolve", mode: "timeline" }));
  expect(screen.getByRole("button", { name: "Send to Resolve (Timeline)" })).toBeInTheDocument();
  expect(get(preferredTarget)).toBe("resolve:timeline");
});

it.each(["resolve:timeline:gyro:unchecked", "resolve:timeline:gyro", "resolve:timeline:gyro:rows"])(
  "picks the timeline for a remembered gyro choice (%s)", (remembered) => {
    preferredTarget.set(remembered);  // remembered from before stabilisation moved to Develop
    render(SendAction, { onclick: vi.fn() });
    expect(screen.getByRole("button", { name: "Send to Resolve (Timeline)" })).toBeInTheDocument();
  });
