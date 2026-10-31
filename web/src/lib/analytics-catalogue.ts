// Shared choices for Analytics and the preview overlay. Availability is measured per recording.
export const ANALYTICS_VIEWS = [
  { key: "gyro", label: "Gyro", source: "imu" },
  { key: "acceleration", label: "Acceleration", source: "imu" },
  { key: "blur", label: "Shake blur", source: "imu" },
  { key: "timing", label: "Frame intervals", source: "video" },
  { key: "wb", label: "White balance", source: "raw" },
  { key: "shading", label: "Lens shading", source: "raw" },
  { key: "noise_scale", label: "Noise scale", source: "raw" },
  { key: "noise_offset", label: "Noise offset", source: "raw" },
  { key: "iso", label: "ISO", source: "raw" },
  { key: "shutter", label: "Shutter duration", source: "raw" },
  { key: "focus", label: "Focus distance", source: "raw" },
  { key: "black", label: "Mean black level", source: "raw" },
  { key: "white", label: "White level", source: "raw" },
] as const;

// Sensor identities keep their colours when graphs or preview modes change.
export const CHART_COLOURS = ["#5fd4c4", "#e8b86d", "#b5a0f5", "#82b8ff", "#f28d85", "#a7d581"];
export const CHANNEL_COLOURS: Record<string, string> = {
  R: "#f28d85", G: "#a7d581", Gr: "#a7d581", Gb: "#5fd4c4", B: "#82b8ff",
  X: "#5fd4c4", Y: "#e8b86d", Z: "#b5a0f5",
};
export const CHART_DASHES = ["", "8 3", "2 3", "8 3 2 3", "12 3", "4 3"];
export function channelColour(label: string, index = 0): string {
  const channel = label.startsWith("G (R row)") ? "Gr" : label.startsWith("G (B row)") ? "Gb" : label.split(" ")[0]!;
  return CHANNEL_COLOURS[channel] ?? CHART_COLOURS[index % CHART_COLOURS.length]!;
}

// A sequential scale with increasing lightness, shared by the calibration and frame maps.
export const HEATMAP_STOPS = ["#243651", "#286779", "#44a998", "#a1d5a0", "#f1e5a0"];
export function heatmapColour(value: number, low: number, high: number): string {
  const t = high === low ? .5 : Math.max(0, Math.min(1, (value - low) / (high - low)));
  const position = t * (HEATMAP_STOPS.length - 1), index = Math.floor(position);
  if (position === index) return HEATMAP_STOPS[index]!;
  const a = HEATMAP_STOPS[index]!, b = HEATMAP_STOPS[index + 1]!;
  return "#" + [1, 3, 5].map(offset => Math.round(parseInt(a.slice(offset, offset + 2), 16) * (1 - position + index)
    + parseInt(b.slice(offset, offset + 2), 16) * (position - index)).toString(16).padStart(2, "0")).join("");
}
