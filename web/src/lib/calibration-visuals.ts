// All plots use recorded metadata. They never request or decode a video frame.
export const BAYER_PLANES = ["R", "G (R row)", "G (B row)", "B"];
export type GainMaps = { width: number; height: number; planes: number[][]; min: number; max: number; sampled: boolean };
export function gainMaps(values: Record<string, unknown>): GainMaps | null {
  const width = values.lensShadingMapWidth, height = values.lensShadingMapHeight, planes = values.lensShadingMap;
  if (typeof width !== "number" || typeof height !== "number" || !Number.isInteger(width) || !Number.isInteger(height)
    || width < 2 || height < 2 || width > 128 || height > 128 || !Array.isArray(planes) || planes.length !== 4) return null;
  let min = Infinity, max = -Infinity;
  for (const plane of planes) {
    if (!Array.isArray(plane) || plane.length !== width * height) return null;
    for (const gain of plane) {
      if (typeof gain !== "number" || !Number.isFinite(gain) || gain <= 0 || gain > 256) return null;
      min = Math.min(min, gain); max = Math.max(max, gain);
    }
  }
  const w = Math.min(width, 24), h = Math.min(height, 24);
  return { width: w, height: h, min, max, sampled: w !== width || h !== height,
    planes: planes.map(plane => Array.from({ length: w * h }, (_, i) => plane[Math.round(Math.floor(i / w) * (height - 1) / (h - 1)) * width + Math.round((i % w) * (width - 1) / (w - 1))] as number)) };
}
export function noiseCurves(values: Record<string, unknown>): { paths: string[]; max: number } | null {
  const pairs = values.noiseProfile;
  if (!Array.isArray(pairs) || pairs.length !== 8 || pairs.some(n => typeof n !== "number" || !Number.isFinite(n) || n < 0)) return null;
  const max = Math.max(...[0, 1, 2, 3].map(i => 100 * Math.sqrt(pairs[2 * i] + pairs[2 * i + 1]))) || 1;
  return { max, paths: [0, 1, 2, 3].map(i => Array.from({ length: 33 }, (_, x) => {
    const noise = 100 * Math.sqrt(pairs[2 * i] * x / 32 + pairs[2 * i + 1]);
    return `${x ? "L" : "M"}${30 + x * 260 / 32},${110 - noise / max * 100}`;
  }).join(" ")) };
}
