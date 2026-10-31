import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import { get } from "svelte/store";
import TrimBar from "../src/components/TrimBar.svelte";
import { obscuring } from "../src/lib/obscuring";

afterEach(cleanup);
it("reserves the edge controls above native video and reveals markers for keyboard users", async () => {
  const seek = vi.fn(), trim = vi.fn();
  const view = render(TrimBar, { edge: true, index: 20, start: 20, end: 80, count: 100, rate: 24, onseek: seek, ontrim: trim });
  const bar = view.container.querySelector(".bar")!;
  expect(get(obscuring).has(bar as HTMLElement)).toBe(true);
  await fireEvent.focusIn(screen.getByRole("slider", { name: "In mark" }));
  expect(bar).toHaveClass("expanded");
  await fireEvent.input(screen.getByRole("slider", { name: "Position" }), { target: { value: "40" } });
  expect(seek).toHaveBeenCalledWith(40);
  expect(trim).not.toHaveBeenCalled();
  await fireEvent.keyDown(screen.getByRole("slider", { name: "In mark" }), { key: "ArrowRight", shiftKey: true });
  expect(trim).toHaveBeenCalledWith(44, 80);
  view.unmount();expect(get(obscuring).size).toBe(0);
});

it("draws the clip's sound under the marks, louder parts taller, the marked part picked out", () => {
  const view = render(TrimBar, { edge: true, index: 0, start: 25, end: 75, count: 100, rate: 24, onseek: vi.fn(), peaks: [0, 1, 0.0039811, 1] });
  const [all, kept] = [...view.container.querySelectorAll<SVGElement>("svg.wave")];
  expect(all).toHaveAttribute("aria-hidden", "true");
  expect(all).toHaveAttribute("viewBox", "0 0 4 100");
  // On a decibel scale from -48 dB: silence is flat, full scale fills the height, -48 dB is flat too.
  expect(all!.querySelector("path")).toHaveAttribute("d", "M0.5,50.0L1.5,0.0L2.5,50.0L3.5,0.0L3.5,100.0L2.5,50.0L1.5,100.0L0.5,50.0Z");
  expect(kept).toHaveClass("kept");
  expect(view.container.querySelector(".bar")).toHaveStyle({ "--range-start": "25%", "--range-end": "75%" });
});

it("draws nothing for a clip without sound", () => {
  const view = render(TrimBar, { edge: true, index: 0, start: 0, end: 100, count: 100, rate: 24, onseek: vi.fn(), peaks: [] });
  expect(view.container.querySelector("svg.wave")).toBeNull();
});
