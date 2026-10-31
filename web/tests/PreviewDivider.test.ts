import { fireEvent, render, screen } from "@testing-library/svelte";
import { expect, it } from "vitest";
import PreviewDivider, { savedPreviewHeight } from "../src/components/PreviewDivider.svelte";

it("resizes the preview from the keyboard within the pane and remembers the height", async () => {
  const pane = document.createElement("div"), viewer = document.createElement("div"), picture = document.createElement("div");
  viewer.className = "viewer"; pane.append(viewer); viewer.append(picture);
  Object.defineProperty(pane, "clientHeight", { value: 700 });
  viewer.getBoundingClientRect = () => ({ height: 400 }) as DOMRect;
  picture.getBoundingClientRect = () => ({ height: 300 }) as DOMRect;
  let height: number | null = null;
  render(PreviewDivider, { props: { get height() { return height; }, set height(value) { height = value; }, pane, picture: () => picture } });
  const divider = screen.getByRole("separator", { name: "Resize preview" });
  await fireEvent.keyDown(divider, { key: "ArrowUp" });
  expect(height).toBe(280);
  expect(savedPreviewHeight()).toBe(280);
  await fireEvent.keyDown(divider, { key: "End" });
  expect(height).toBe(440); // 700 less the 100 of controls and 160 kept for the settings
});

it("uses the height saved last, and none before one is saved", () => {
  localStorage.setItem("mcrawsome.preview-height", "300");
  expect(savedPreviewHeight()).toBe(300);
  localStorage.removeItem("mcrawsome.preview-height");
  expect(savedPreviewHeight()).toBeNull();
});
