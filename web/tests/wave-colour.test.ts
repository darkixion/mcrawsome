import { expect, it } from "vitest";
import { waveColours } from "../src/lib/wave-colour";

const clip = (...levels: number[]) => levels.map(level => 10 ** (level / 20)); // parts at these decibels
function brightness(hex: string): number {
  const [red, green, blue] = [1, 3, 5].map(at => parseInt(hex.slice(at, at + 2), 16)) as [number, number, number];
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}
const colours = (stops: { colour: string }[]) => stops.map(stop => stop.colour);

it("gives each part one stop at its middle", () => {
  const { all, kept } = waveColours(clip(-40, -30, -20, -10));
  for (const stops of [all, kept]) expect(stops.map(stop => stop.offset)).toEqual([0.125, 0.375, 0.625, 0.875]);
});

it("makes a clip's louder parts brighter, marked or not", () => {
  const { all, kept } = waveColours(clip(-40, -30, -20, -10));
  for (const stops of [all, kept]) {
    const shown = colours(stops).map(brightness);
    expect(shown).toEqual([...shown].sort((a, b) => a - b));
    expect(new Set(shown).size).toBe(4);
  }
});

it("picks out the marked part at every level", () => {
  const { all, kept } = waveColours(clip(-40, -30, -20, -10));
  kept.forEach((stop, part) => expect(brightness(stop.colour)).toBeGreaterThan(brightness(all[part]!.colour)));
});

it("goes by the clip's own range, so a quiet clip's loud parts look like a loud clip's", () => {
  expect(waveColours(clip(-46, -36, -26))).toEqual(waveColours(clip(-20, -10, 0)));
});

it("does not let one clap leave the rest of the clip dark", () => {
  const talking = Array.from({ length: 99 }, (_, part) => part % 2 ? -20 : -30);
  const { kept } = waveColours(clip(...talking, 0));
  expect(kept[1]!.colour).toBe(kept[99]!.colour); // the louder talking is as bright as the clap
});

it("does not stretch an even clip over the whole range of colours", () => {
  const { kept } = waveColours(clip(-24, -23, -22, -21));
  const { kept: varied } = waveColours(clip(-48, -36, -24, -12));
  expect(brightness(kept[0]!.colour)).toBeGreaterThan(brightness(varied[1]!.colour));
});

it("copes with silence and no sound", () => {
  expect(waveColours([0, 0, 0]).kept).toHaveLength(3);
  expect(waveColours([])).toEqual({ all: [], kept: [] });
});
