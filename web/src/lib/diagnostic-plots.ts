import type { Schemas } from "./api";
export const PLANE_COLOURS = ["#ee9292", "#a7d581", "#65c9b5", "#8cbcff"];
export function histogramPaths(report: Schemas["RawHistogram"], logarithmic: boolean): { paths: string[]; peak: number } {
  const peak = Math.max(1, ...report.planes.flatMap(plane => plane.bins));
  const scale = (value: number) => logarithmic ? Math.log1p(value) / Math.log1p(peak) : value / peak;
  return { peak, paths: report.planes.map(plane => plane.bins.map((count, i) => `${i ? "L" : "M"}${30 + (i + .5) * 260 / plane.bins.length},${110 - 100 * scale(count)}`).join(" ")) };
}
export function timingPlot(report: Schemas["TimingDiagnostics"], zoomed = false): { y: (value: number) => number; x: (index: number) => number; path: string; low: number; high: number } {
  const minimum = report.minimum_ms ?? 0, maximum = report.maximum_ms ?? 1;
  const padding = Math.max(.001, (maximum - minimum) * .05);
  const low = zoomed ? minimum - padding : Math.min(0, minimum);
  const high = zoomed ? maximum + padding : Math.max(1, maximum);
  const y = (value: number) => 110 - (value - low) / (high - low) * 100;
  const x = (frame: number) => 30 + (frame - 1) / Math.max(1, report.intervals - 1) * 260;
  return { y, x, low, high, path: report.buckets.map((bucket, i) => `${i ? "L" : "M"}${x((bucket.first_frame + bucket.last_frame) / 2)},${y(bucket.mean_ms)}`).join(" ") };
}
