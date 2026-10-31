import { render } from "@testing-library/svelte";
import { createRawSnippet } from "svelte";
import { afterEach, expect, it, vi } from "vitest";
import Warp from "../src/components/Warp.svelte";
import { homography, keptTransform } from "../src/lib/warp";

const apply = (h: number[], [x, y]: [number, number]) => {
  const w = h[6]! * x + h[7]! * y + h[8]!;
  return [(h[0]! * x + h[1]! * y + h[2]!) / w, (h[3]! * x + h[4]! * y + h[5]!) / w];
};

it("takes each of four points to its partner", () => {
  const from: [number, number][] = [[20, 10], [95, 15], [90, 95], [15, 90]];
  const to: [number, number][] = [[0, 0], [100, 0], [100, 100], [0, 100]];
  const h = homography(from, to);
  from.forEach((point, i) => {
    const [x, y] = apply(h, point);
    expect(x).toBeCloseTo(to[i]![0], 9);
    expect(y).toBeCloseTo(to[i]![1], 9);
  });
});

it("zooms a centred kept frame about the centre, and clips a letterboxed picture to itself", () => {
  const centred = [0.1, 0.1, 0.9, 0.1, 0.9, 0.9, 0.1, 0.9];
  const fitted = keptTransform(centred, 2, 1000, 500);
  const numbers = fitted.transform.slice("matrix3d(".length, -1).split(",").map(Number);
  expect(numbers[0]).toBeCloseTo(1.25);  // 1 / 0.8
  expect(numbers[5]).toBeCloseTo(1.25);
  expect(numbers[12]).toBeCloseTo(-125);  // the centre stays put: 500 - 1.25 * 500
  expect(numbers[13]).toBeCloseTo(-62.5);
  expect(fitted.clip).toBe("inset(0px 0px)");
  expect(keptTransform(centred, 2, 1200, 500).clip).toBe("inset(0px 100px)");  // bars at the sides
});

afterEach(() => vi.unstubAllGlobals());

it("warps the picture only while a frame's kept frame is given", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1000);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(500);
  const children = createRawSnippet(() => ({ render: () => "<img alt=\"Clip preview\" />" }));
  const view = render(Warp, { width: 2000, height: 1000, corners: null, children });
  const content = view.container.querySelector(".content") as HTMLElement;
  expect(content.style.transform).toBe("");
  await view.rerender({ width: 2000, height: 1000, corners: [0.1, 0.1, 0.9, 0.1, 0.9, 0.9, 0.1, 0.9], children });
  expect(content.style.transform).toMatch(/^matrix3d\(1\.25/);
  expect(view.container.querySelector(".warp")).toHaveAttribute("data-stabilised", "true");
  await view.rerender({ width: 2000, height: 1000, corners: null, children });
  expect(content.style.transform).toBe("");
  vi.restoreAllMocks();
});
