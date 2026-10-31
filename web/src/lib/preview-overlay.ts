import type { Schemas } from "./api";
import { channelColour } from "./analytics-catalogue";
import { chartEnvelope } from "./analytics-chart";

export type OverlayPoint = { x: number; y: number | null; low: number | null; high: number | null };
export type OverlayLine = { label: string; colour: string; points: OverlayPoint[] };
export type OverlayModel = { title: string; note: string; frames: number; lines: OverlayLine[] };
export function overlayModel(chart: Schemas["OverlayChart"], frames: number): OverlayModel {
  const values = chart.series.flatMap(series => series.points.flatMap(p => [p.value, p.minimum, p.maximum].filter((v): v is number => v != null)));
  const low = Math.min(...values), high = Math.max(...values);
  const padding = Math.max((high - low) * .08, Math.abs(high) * .005, 1e-12);
  const y = (value: number | null | undefined) => value == null ? null : 1 - (value - low + padding) / (high - low + 2 * padding);
  return { title: `${chart.label} · ${chart.unit} · ${low.toPrecision(3)}–${high.toPrecision(3)}`, note: chart.help, frames,
    lines: chart.series.map((series, i) => ({ label: series.label, colour: channelColour(series.label, i), points: series.points.map(p => ({
      x: p.frame / Math.max(1, frames - 1), y: y(p.value), low: y(p.minimum), high: y(p.maximum),
    })) })),
  };
}
export function overlayProgress(index: number, frames: number): number { return frames <= 1 ? 1 : Math.max(0, Math.min(1, index / (frames - 1))); }
export function overlayPath(points: OverlayPoint[]): string {
  let connected = false;
  return points.map(point => {
    if (point.y === null) { connected = false; return ""; }
    const result = `${connected ? "L" : "M"}${point.x * 1000},${point.y * 100}`;
    connected = true; return result;
  }).join(" ");
}

export function overlayEnvelope(points: OverlayPoint[]): string {
  return chartEnvelope(points.map(point => ({ x: point.x, value: point.y, minimum: point.low, maximum: point.high })), x => x * 1000, y => y * 100);
}
