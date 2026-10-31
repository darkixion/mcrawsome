import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import Harness from "./ViewportHarness.svelte";
import DockHarness from "./DockHarness.svelte";

afterEach(() => vi.restoreAllMocks());
it("keeps the picture and controls together through zoom changes and fullscreen exit", async () => {
  let fullscreen: Element | null = null;
  Object.defineProperty(document, "fullscreenElement", { configurable: true, get: () => fullscreen });
  const request = vi.fn(function(this: HTMLElement) { fullscreen = this; document.dispatchEvent(new Event("fullscreenchange")); return Promise.resolve(); });
  Object.defineProperty(HTMLElement.prototype, "requestFullscreen", { configurable: true, value: request });
  const exit = vi.fn(() => { fullscreen = null; document.dispatchEvent(new Event("fullscreenchange")); return Promise.resolve(); });
  Object.defineProperty(document, "exitFullscreen", { configurable: true, value: exit });
  const { container, unmount } = render(Harness);
  const picture = screen.getByTestId("picture");
  const zoom = screen.getByRole("button", { name: "Zoom" });
  expect(zoom).toHaveTextContent("Fit");
  await fireEvent.click(zoom);
  expect(screen.getAllByRole("menuitemradio").map(item => item.textContent)).toEqual(["Fit", "25%", "50%", "75%", "100% (actual size)"]);
  await fireEvent.click(screen.getByRole("menuitemradio", { name: "50%" }));
  expect(container.querySelector(".image-surface")).toHaveStyle({ width: "1920px", height: "1080px" });
  expect(screen.queryByRole("menu")).toBeNull();
  await fireEvent.click(zoom);
  await fireEvent.click(screen.getByRole("menuitemradio", { name: "100% (actual size)" }));
  expect(container.querySelector(".image-surface")).toHaveStyle({ width: "3840px", height: "2160px" });
  const enter = screen.getByRole("button", { name: "Enter fullscreen" });
  expect(enter).toHaveAttribute("title", "Enter fullscreen (Ctrl+F)");
  await fireEvent.click(enter);
  await screen.findByRole("button", { name: "Exit fullscreen" });
  expect(document.fullscreenElement?.contains(picture)).toBe(true);
  expect(document.fullscreenElement?.contains(screen.getByRole("button", { name: "Play" }))).toBe(true);
  expect(document.fullscreenElement?.contains(screen.getByRole("button", { name: "Tool" }))).toBe(true);
  expect(document.fullscreenElement?.contains(screen.getByRole("slider", { name: "Position" }))).toBe(true);
  expect(document.fullscreenElement?.contains(screen.getByRole("slider", { name: "In mark" }))).toBe(true);
  expect(document.fullscreenElement?.contains(screen.getByRole("slider", { name: "Out mark" }))).toBe(true);
  expect(screen.getByTestId("picture")).toBe(picture);
  await fireEvent.keyDown(document, { key: "Escape" });
  await waitFor(() => expect(fullscreen).toBeNull());
  await fireEvent.keyDown(document, { key: "f", ctrlKey: true });
  await screen.findByRole("button", { name: "Exit fullscreen" });
  await fireEvent.keyDown(document, { key: "F", ctrlKey: true });
  await waitFor(() => expect(fullscreen).toBeNull());
  await fireEvent.click(screen.getByRole("button", { name: "Enter fullscreen" }));
  await screen.findByRole("button", { name: "Exit fullscreen" });
  await fireEvent.click(screen.getByRole("button", { name: "Exit fullscreen" }));
  await waitFor(() => expect(fullscreen).toBeNull());
  expect(zoom).toHaveTextContent("100%");
  unmount();
  delete (HTMLElement.prototype as Partial<HTMLElement>).requestFullscreen;
  delete (document as Partial<Document>).exitFullscreen;
  Reflect.deleteProperty(document, "fullscreenElement");
});

it("recovers a desktop fullscreen request rejected after user activation was lost", async () => {
  let fullscreen: Element | null = null;
  Object.defineProperty(document, "fullscreenElement", { configurable: true, get: () => fullscreen });
  Object.defineProperty(HTMLElement.prototype, "requestFullscreen", { configurable: true, value: vi.fn().mockRejectedValue(new TypeError("Cannot request fullscreen without transient activation")) });
  const request = vi.fn(async (id: string) => {
    fullscreen = document.getElementById(id);
    document.dispatchEvent(new Event("fullscreenchange"));
    return !!fullscreen;
  });
  window.pywebview = { api: { request_fullscreen: request, choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn() } };
  window.dispatchEvent(new Event("pywebviewready"));
  const { unmount } = render(Harness);
  await fireEvent.click(screen.getByRole("button", { name: "Enter fullscreen" }));
  await screen.findByRole("button", { name: "Exit fullscreen" });
  expect(request).toHaveBeenCalledOnce();
  expect(fullscreen).toHaveAttribute("data-mcrawsome-viewport", "true");
  expect(screen.queryByText("Could not change fullscreen mode")).toBeNull();
  fullscreen = null;
  document.dispatchEvent(new Event("fullscreenchange"));
  unmount();
  delete window.pywebview;
  window.dispatchEvent(new Event("pywebviewready"));
  delete (HTMLElement.prototype as Partial<HTMLElement>).requestFullscreen;
  Reflect.deleteProperty(document, "fullscreenElement");
});

it("shows a real fullscreen failure when the native retry is rejected too", async () => {
  Object.defineProperty(HTMLElement.prototype, "requestFullscreen", { configurable: true, value: vi.fn().mockRejectedValue(new TypeError("Rejected")) });
  window.pywebview = { api: { request_fullscreen: vi.fn().mockResolvedValue(false), choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn() } };
  window.dispatchEvent(new Event("pywebviewready"));
  const { unmount } = render(Harness);
  await fireEvent.click(screen.getByRole("button", { name: "Enter fullscreen" }));
  expect(await screen.findByText("Could not change fullscreen mode")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Enter fullscreen" })).toBeInTheDocument();
  unmount();delete window.pywebview;window.dispatchEvent(new Event("pywebviewready"));
  delete (HTMLElement.prototype as Partial<HTMLElement>).requestFullscreen;
});

it("offers moving the picture to the clip list beside fullscreen, and moving it back", async () => {
  const ondock = vi.fn();
  render(DockHarness, { ondock });
  const button = screen.getByRole("button", { name: "Move the preview to the clip list" });
  expect(button.nextElementSibling).toBe(screen.getByRole("button", { name: "Enter fullscreen" }));
  await fireEvent.click(button);
  expect(ondock).toHaveBeenCalledTimes(1);
  const back = screen.getByRole("button", { name: "Move the preview back" });
  expect(back).toHaveAttribute("title", "Put the preview back above the settings and show the clips");
  await fireEvent.click(back);
  expect(ondock).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("button", { name: "Move the preview to the clip list" })).toBeInTheDocument();
});

it("has no dock button where there is no clip list", () => {
  render(DockHarness);
  expect(screen.queryByRole("button", { name: /Move the preview/ })).toBeNull();
});
