export type ChartSample = { x: number; value: number | null; minimum?: number | null; maximum?: number | null; frame?: number | null; firstFrame?: number; lastFrame?: number; time?: number | null };
export type ChartLine = { label: string; colour: string; unit: string; samples: ChartSample[]; visible?: boolean };
export const CHART = { left: 72, right: 506, top: 16, bottom: 182, width: 520, height: 228 };

export function niceScale(minimum: number, maximum: number, divisions = 4) {
  if (!Number.isFinite(minimum) || !Number.isFinite(maximum)) { minimum = 0; maximum = 1; }
  if (minimum === maximum) { const pad = Math.abs(minimum) * .05 || 1; minimum -= pad; maximum += pad; }
  const raw = (maximum - minimum) / divisions;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const factor = raw / magnitude;
  const step = (factor <= 1 ? 1 : factor <= 2 ? 2 : factor <= 2.5 ? 2.5 : factor <= 5 ? 5 : 10) * magnitude;
  const low = Math.floor(minimum / step) * step, high = Math.ceil(maximum / step) * step;
  const count = Math.round((high - low) / step);
  const ticks = Array.from({ length: Math.min(20, count + 1) }, (_, index) => {
    const value = low + step * index;
    return Math.abs(value) < step * 1e-9 ? 0 : value;
  });
  return { low, high, step, ticks };
}
export function chartNumber(value: number): string {
  if (!Number.isFinite(value)) return "Unavailable";
  if (value === 0) return "0";
  if (Math.abs(value) < .001 || Math.abs(value) >= 1e7) return value.toExponential(2);
  return Number(value.toPrecision(6)).toLocaleString("en", { maximumFractionDigits: 8 });
}
// Axis precision follows the tick spacing, including very small changes around a nonzero baseline.
export function chartTick(value: number, step: number): string {
  if (!Number.isFinite(value) || !Number.isFinite(step) || step <= 0) return chartNumber(value);
  if (value === 0) return "0";
  const digits = Math.min(12, Math.max(0, -Math.floor(Math.log10(step))) + 1);
  if (Math.abs(value) < .001 || Math.abs(value) >= 1e7) {
    const precision = Math.min(12, Math.max(2, Math.ceil(Math.log10(Math.abs(value) / step)) + 1));
    return value.toExponential(precision);
  }
  return value.toLocaleString("en", { maximumFractionDigits: digits });
}
export function chartValue(value: number): string { return Number.isFinite(value) ? String(Number(value.toPrecision(8))) : "Unavailable"; }
export function nearestSample(samples: ChartSample[], x: number): number {
  if (!samples.length) return -1;
  let low = 0, high = samples.length;
  while (low < high) { const middle = (low + high) >>> 1; if (samples[middle]!.x < x) low = middle + 1; else high = middle; }
  if (low === samples.length) return samples.length - 1;
  return low > 0 && x - samples[low - 1]!.x <= samples[low]!.x - x ? low - 1 : low;
}
export function chartPath(samples: ChartSample[], x: (v: number) => number, y: (v: number) => number): string {
  let connected = false;
  return samples.map(sample => {
    if (sample.value === null || !Number.isFinite(sample.value)) { connected = false; return ""; }
    const command = `${connected ? "L" : "M"}${x(sample.x).toFixed(2)},${y(sample.value).toFixed(2)}`;
    connected = true; return command;
  }).join(" ");
}

// Straight segments preserve the recorded group positions and extrema. Never bridge a missing mean.
export function chartEnvelope(samples: ChartSample[], x: (v: number) => number, y: (v: number) => number): string {
  const paths: string[] = [];
  let group: ChartSample[] = [];
  function close(): void {
    if (group.length > 1) paths.push(group.map((point, index) => `${index ? "L" : "M"}${x(point.x).toFixed(2)},${y(point.maximum!).toFixed(2)}`).join(" ")
      + " " + [...group].reverse().map(point => `L${x(point.x).toFixed(2)},${y(point.minimum!).toFixed(2)}`).join(" ") + " Z");
    group = [];
  }
  for (const point of samples) {
    if (point.value == null || !Number.isFinite(point.value) || point.minimum == null || point.maximum == null || !Number.isFinite(point.minimum) || !Number.isFinite(point.maximum)) close();
    else group.push(point);
  }
  close(); return paths.join(" ");
}
export function chartOutliers(samples: ChartSample[], median: number | null): { x: number; value: number; kind: string }[] {
  if (median == null || median <= 0 || !Number.isFinite(median)) return [];
  return samples.flatMap(point => {
    if (point.value == null || !Number.isFinite(point.value)) return [];
    const low = point.minimum ?? point.value, high = point.maximum ?? point.value;
    const markers = [];
    if (high > median * 1.5) markers.push({ x: point.x, value: high, kind: "Long interval" });
    if (low < median * .5) markers.push({ x: point.x, value: low, kind: low <= 0 ? "Non-positive interval" : "Short interval" });
    return markers;
  });
}

export function isolatedPoints<T>(points: T[], value: (point: T) => number | null): T[] {
  const valid = (index: number) => points[index] != null && value(points[index]!) != null && Number.isFinite(value(points[index]!));
  return points.filter((_, index) => valid(index) && !valid(index - 1) && !valid(index + 1));
}
