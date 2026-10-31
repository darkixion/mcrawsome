import { render, screen } from "@testing-library/svelte";
import { expect, it, vi } from "vitest";
import ScrubCanvas, { type Painter } from "../src/components/ScrubCanvas.svelte";

it("draws synchronously for each call, reuses the surface, and hides it after rest", async () => {
  const drawImage = vi.fn();
  const context = vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D);
  const onpainter = vi.fn();
  const view = render(ScrubCanvas, { active: true, onpainter });
  const paint = onpainter.mock.calls[0]![0] as Painter;
  const image = new Image();
  image.src = "blob:ready";
  Object.defineProperties(image, { naturalWidth: { value: 32 }, naturalHeight: { value: 16 } });
  const canvas = screen.getByLabelText("Scrub preview") as HTMLCanvasElement;
  for (let move = 1; move <= 4; move++) {
    paint(image, { kind: "draft", index: 2 });
    expect(drawImage).toHaveBeenCalledTimes(move);
    expect(canvas.dataset.draw).toBe(String(move));
    expect(canvas.style.display).toBe("block");
    expect(canvas.width).toBe(32);
    expect(canvas.height).toBe(16);
  }
  expect(context).toHaveBeenCalledTimes(1);
  await view.rerender({ active: false });
  expect(canvas.style.display).toBe("none");
  view.unmount();
  expect(onpainter).toHaveBeenLastCalledWith(null);
  context.mockRestore();
});

it("bounds the drag surface when reusing a larger resting frame", () => {
  const drawImage = vi.fn();
  const context = vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D);
  const onpainter = vi.fn();
  const view = render(ScrubCanvas, { active: true, onpainter });
  const paint = onpainter.mock.calls[0]![0] as Painter;
  const image = new Image();
  Object.defineProperties(image, { naturalWidth: { value: 1920 }, naturalHeight: { value: 1080 } });
  paint(image, { kind: "draft", index: 2 });
  const canvas = screen.getByLabelText("Scrub preview") as HTMLCanvasElement;
  expect(canvas.width).toBe(640);
  expect(canvas.height).toBe(360);
  expect(drawImage).toHaveBeenCalledWith(image, 0, 0, 640, 360);
  view.unmount();
  context.mockRestore();
});
