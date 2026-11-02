// The colour of each part of the clip's sound on the trim bar, from how loud it is within the clip: dark for its quiet
// parts and brightest for its loud ones, so that a clap or the start of speech stands out in a quiet clip as in a loud
// one. The marked part is teal, passing through the accent colour, and the rest blue-grey, which keeps the marks clear.
export type WaveStop = { offset: number; colour: string };
type Ramp = [number, string][]; // ascending shares of the clip's range and their colours

const MARKED: Ramp = [[0, "#1c5650"], [0.4, "#319186"], [0.75, "#5fd4c4"], [1, "#e4fbf7"]];
const UNMARKED: Ramp = [[0, "#2e383f"], [0.4, "#46525a"], [0.75, "#66737c"], [1, "#8d99a1"]];
const FLOOR = -48; // decibels: the waveform's height is drawn from here to full scale
const SMALLEST_RANGE = 12; // decibels: the sound of an even clip is not stretched over the whole ramp

const rgb = (hex: string): number[] => [1, 3, 5].map(at => parseInt(hex.slice(at, at + 2), 16));
function colourAt(ramp: Ramp, position: number): string {
  const after = ramp.findIndex(([at]) => at >= position);
  if (after <= 0) return ramp[after === 0 ? 0 : ramp.length - 1]![1];
  const [fromAt, from] = ramp[after - 1]!, [toAt, to] = ramp[after]!;
  const share = (position - fromAt) / (toAt - fromAt), target = rgb(to);
  return `#${rgb(from).map((value, i) => Math.round(value + (target[i]! - value) * share).toString(16).padStart(2, "0")).join("")}`;
}

// From the clip's quietest parts to nearly its loudest, so that one clap does not leave the rest of it all dark.
function clipRange(levels: number[]): [number, number] {
  const sorted = [...levels].sort((a, b) => a - b);
  const low = Math.max(FLOOR, sorted[Math.floor(sorted.length * 0.05)]!);
  const high = sorted[Math.floor(sorted.length * 0.98)]!;
  return [Math.min(low, high - SMALLEST_RANGE), high];
}

// One gradient stop at the middle of each part, for everything (`all`) and for the marked part drawn over it (`kept`).
export function waveColours(peaks: number[]): { all: WaveStop[]; kept: WaveStop[] } {
  const levels = peaks.map(peak => 20 * Math.log10(Math.max(peak, 1e-6)));
  const [low, high] = clipRange(levels);
  const along = (ramp: Ramp): WaveStop[] => levels.map((level, part) => ({
    offset: (part + 0.5) / peaks.length, colour: colourAt(ramp, Math.min(1, Math.max(0, (level - low) / (high - low)))),
  }));
  return { all: along(UNMARKED), kept: along(MARKED) };
}
