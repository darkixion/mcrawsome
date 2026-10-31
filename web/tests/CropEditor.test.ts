import { fireEvent, render } from "@testing-library/svelte";
import { expect, it, vi } from "vitest";
import CropEditor from "../src/components/CropEditor.svelte";

// The picture is 400 x 225 screen pixels for a 3840 x 2160 frame: 9.6 picture pixels per screen pixel.
function editor(aspect: number | null = null) {
  const onchange = vi.fn();
  const view = render(CropEditor, { width: 3840, height: 2160, box: { left: 960, top: 540, width: 1920, height: 1080 }, aspect, onchange });
  const svg = view.container.querySelector("svg")!;
  svg.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 400, bottom: 225, width: 400, height: 225 }) as DOMRect;
  svg.setPointerCapture = () => {};
  return { svg, onchange };
}

it("moves the crop when dragged from inside and keeps it on the picture", async () => {
  const { svg, onchange } = editor();
  await fireEvent.pointerDown(svg, { clientX: 200, clientY: 112, button: 0, pointerId: 1 });
  await fireEvent.pointerMove(svg, { clientX: 400, clientY: 112, pointerId: 1 });
  expect(onchange).toHaveBeenLastCalledWith({ left: 1920, top: 540, width: 1920, height: 1080 });
});

it("resizes from an edge, and holds the chosen shape from a corner", async () => {
  const { svg, onchange } = editor();
  await fireEvent.pointerDown(svg, { clientX: 300, clientY: 112, button: 0, pointerId: 1 }); // the right edge
  await fireEvent.pointerMove(svg, { clientX: 250, clientY: 112, pointerId: 1 });
  expect(onchange).toHaveBeenLastCalledWith({ left: 960, top: 540, width: 1440, height: 1080 });
  await fireEvent.pointerUp(svg, { pointerId: 1 });
  const square = editor(1);
  await fireEvent.pointerDown(square.svg, { clientX: 300, clientY: 169, button: 0, pointerId: 2 }); // bottom-right corner
  await fireEvent.pointerMove(square.svg, { clientX: 250, clientY: 160, pointerId: 2 });
  const [box] = square.onchange.mock.lastCall!;
  expect(box.width).toBeCloseTo(box.height);
  expect([box.left, box.top]).toEqual([960, 540]);
});
