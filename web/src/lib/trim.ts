// The CLI accepts frames, seconds and colon-separated times. Keep existing marks when opening a clip.
export function timeSeconds(value: string | null | undefined, rate: number): number {
  if (!value) return 0;
  if (value.endsWith("f")) return Number(value.slice(0, -1)) / rate;
  const parts = value.replace(/s$/, "").split(":").map(Number);
  return parts.reduce((seconds, part) => seconds * 60 + part, 0);
}
export function secondsSyntax(seconds: number): string { return `${Number(seconds.toFixed(6))}s`; }
