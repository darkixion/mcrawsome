import type { Schemas } from "./api";
import { channelColour } from "./analytics-catalogue";
export const GYRO_COLOURS = ["X", "Y", "Z"].map(axis => channelColour(axis));
export function gyroPaths(groups: Schemas["GyroBucket"][], duration: number) {
  const bound = Math.max(1e-6, ...groups.flatMap(group => [...group.minimum, ...group.maximum].map(Math.abs)));
  const x = (time: number) => 38 + 264 * time / Math.max(duration, 1e-6);
  const y = (value: number) => 72 - 54 * value / bound;
  const point = (time: number, value: number) => `${x(time).toFixed(2)},${y(value).toFixed(2)}`;
  return { bound, series: [0, 1, 2].map(axis => ({
    mean: groups.map((g, i) => `${i ? "L" : "M"}${point((g.start_s + g.end_s) / 2, g.mean[axis] ?? 0)}`).join(" "),
    envelope: groups.map((g, i) => `${i ? "L" : "M"}${point((g.start_s + g.end_s) / 2, g.maximum[axis] ?? 0)}`).join(" ")
      + [...groups].reverse().map(g => `L${point((g.start_s + g.end_s) / 2, g.minimum[axis] ?? 0)}`).join(" ") + " Z",
  })) };
}
