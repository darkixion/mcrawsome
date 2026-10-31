import { describe, expect, it } from "vitest";
import { flatField } from "../src/lib/flat-field";

describe("flatField", () => {
  it("shows the least corrected site at full brightness and the falloff darker", () => {
    const pixels = flatField([[1, 4], [1, 4], [1, 4], [1, 4]], 2, 1);
    expect([...pixels.slice(0, 4)]).toEqual([255, 255, 255, 255]);
    const corner = Math.round(255 * Math.pow(0.25, 1 / 2.2));
    expect([...pixels.slice(4, 8)]).toEqual([corner, corner, corner, 255]);
  });

  it("tints where one colour is corrected more than the others", () => {
    const pixels = flatField([[2], [1], [1], [1]], 1, 1);
    expect(pixels[0]).toBeLessThan(pixels[1]!);
    expect(pixels[1]).toBe(pixels[2]);
  });

  it("averages the two greens as their mean response", () => {
    const pixels = flatField([[1], [1], [4], [1]], 1, 1);
    expect(pixels[1]).toBe(Math.round(255 * Math.pow((1 + 1 / 4) / 2, 1 / 2.2)));
  });
});
