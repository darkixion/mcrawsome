import { expect, it } from "vitest";
import { fitAspect, formatCrop, parseCrop, snap, toRecorded, toShown } from "../src/lib/dng-crop";

it("reads and writes the engine's crop syntax, placed or from the top-left", () => {
  expect(parseCrop("3840x2160")).toEqual({ width: 3840, height: 2160, left: 0, top: 0 });
  expect(parseCrop("1024x768+64+32")).toEqual({ width: 1024, height: 768, left: 64, top: 32 });
  expect(parseCrop("auto")).toBeNull();
  expect(formatCrop({ width: 1024, height: 768, left: 0, top: 0 })).toBe("1024x768");
  expect(formatCrop({ width: 1024, height: 768, left: 64, top: 32 })).toBe("1024x768+64+32");
});

it("turns a rectangle on the shown picture into recorded pixels and back for every orientation", () => {
  const box = { left: 100, top: 300, width: 600, height: 400 };
  for (const rotation of [0, 90, 180, 270]) {
    const shown = rotation % 180 ? { width: 2160, height: 3840 } : { width: 3840, height: 2160 };
    const recorded = toRecorded(box, rotation, 3840, 2160);
    expect(recorded.left + recorded.width).toBeLessThanOrEqual(3840);
    expect(recorded.top + recorded.height).toBeLessThanOrEqual(2160);
    expect(toShown(recorded, rotation, 3840, 2160)).toEqual(box);
    expect(shown.width * shown.height).toBe(3840 * 2160);
  }
  // Turned clockwise, the shown top-left is the recorded bottom-left (checked against the engine's preview).
  expect(toRecorded({ left: 0, top: 0, width: 1080, height: 1080 }, 90, 3840, 2160)).toEqual({ left: 0, top: 1080, width: 1080, height: 1080 });
  expect(toRecorded({ left: 0, top: 0, width: 1080, height: 1080 }, 270, 3840, 2160)).toEqual({ left: 2760, top: 0, width: 1080, height: 1080 });
});

it("snaps every edge to the proxy step and keeps the crop inside the frame", () => {
  expect(snap({ left: 13, top: 7, width: 1001, height: 499 }, 2, 3840, 2160)).toEqual({ left: 14, top: 8, width: 1002, height: 500 });
  expect(snap({ left: 3800, top: -5, width: 1000, height: 5000 }, 16, 3840, 2160)).toEqual({ left: 2832, top: 0, width: 1008, height: 2160 });
  const fitted = fitAspect({ left: 0, top: 0, width: 3840, height: 2160 }, 4 / 3);
  expect(fitted).toEqual({ left: 480, top: 0, width: 2880, height: 2160 });
});
