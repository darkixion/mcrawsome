import { fireEvent, render, screen } from "@testing-library/svelte";
import { expect, it } from "vitest";
import CropGuide from "../src/components/CropGuide.svelte";
import { obscuringBoxes } from "../src/lib/obscuring";

it("explains the stabilisation crop beside the picture while the margin is hovered", async () => {
  const view = render(CropGuide, { width: 1920, height: 1080, crop: 1.2 });
  const svg = view.container.querySelector("svg")!;
  svg.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 400, bottom: 225, width: 400, height: 225 }) as DOMRect;
  await fireEvent.pointerMove(view.container.querySelector("path")!, { clientX: 200, clientY: 5 });
  const tip = screen.getByRole("tooltip");
  expect(tip).toHaveTextContent("Stabilisation crop (1.20×)");
  expect(tip.style.top).toBe("233px");  // below the picture, so the native picture keeps its area
  expect(obscuringBoxes()).toHaveLength(1);
  await fireEvent.pointerLeave(view.container.querySelector("g")!);
  expect(screen.queryByRole("tooltip")).toBeNull();
  expect(obscuringBoxes()).toHaveLength(0);
});

it("moves the kept frame with the shown frame once the clip is planned", async () => {
  const corners = [[0.125, 0.125, 0.875, 0.125, 0.875, 0.875, 0.125, 0.875], [0.25, 0.125, 0.9375, 0.1875, 0.875, 0.9375, 0.1875, 0.875]];
  const view = render(CropGuide, { width: 1000, height: 500, crop: 1.25, corners, index: 1 });
  expect(screen.getByRole("img", { name: "Stabilise crop 1.25: the outlined frame remains, moving as the stabilisation will" })).toBeInTheDocument();
  expect(view.container.querySelector(".line")).toHaveAttribute("d", "M250 62.5L937.5 93.75L875 468.75L187.5 437.5Z");
  await view.rerender({ width: 1000, height: 500, crop: 1.25, corners, index: 0 });
  expect(view.container.querySelector(".line")).toHaveAttribute("d", "M125 62.5L875 62.5L875 437.5L125 437.5Z");
  await fireEvent.pointerMove(view.container.querySelector("path")!, { clientX: 5, clientY: 5 });
  expect(screen.getByRole("tooltip")).toHaveTextContent("It moves with each frame as Resolve's stabilisation will.");
  await view.rerender({ width: 1000, height: 500, crop: 1.25, corners, index: 0, refining: true });
  expect(screen.getByRole("tooltip")).toHaveTextContent("as Resolve's stabilisation will; refining it with image detail…");
});

it("says while the movement is worked out, and why it cannot be shown", async () => {
  const view = render(CropGuide, { width: 1000, height: 500, crop: 1.25, planning: true });
  expect(view.container.querySelector(".line")).toHaveAttribute("d", "M100 50H900V450H100Z");
  await fireEvent.pointerMove(view.container.querySelector("path")!, { clientX: 5, clientY: 5 });
  expect(screen.getByRole("tooltip")).toHaveTextContent("Working out how it moves with each frame…");
  await view.rerender({ width: 1000, height: 500, crop: 1.25, planning: false, reason: "Stabilisation strength is zero for a.mcraw" });
  expect(screen.getByRole("tooltip")).toHaveTextContent("It cannot show the movement: Stabilisation strength is zero for a.mcraw");
});
