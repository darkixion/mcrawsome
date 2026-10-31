import { expect, it } from "vitest";
import { contrastText } from "../src/lib/contrast";

it("chooses the higher contrast text for the existing semantic chip colours", () => {
  for (const colour of ["#5fd4c4", "#7bd88f", "#f28b82", "#e3b85c"]) expect(contrastText(colour)).toBe("#000");
  expect(contrastText("#263038")).toBe("#fff");
});
it("uses linear light contrast around the black/white crossover", () => {
  expect(contrastText("rgb(117, 117, 117)")).toBe("#fff");
  expect(contrastText("rgb(118, 118, 118)")).toBe("#000");
  expect(contrastText("rgba(0, 0, 0, 0)")).toBeNull();
  expect(contrastText("not a colour")).toBeNull();
});
