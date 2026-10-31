// Formatting of the engine's numbers for display; nothing here computes a value of its own.

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** A clip's length as a clock: 00:18, 10:05, 1:02:05. */
export function formatClock(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  const hours = Math.floor(whole / 3600);
  const minutes = String(Math.floor((whole % 3600) / 60)).padStart(2, "0");
  const rest = String(whole % 60).padStart(2, "0");
  return hours > 0 ? `${hours}:${minutes}:${rest}` : `${minutes}:${rest}`;
}

const SIZES: Record<string, string> = {
  "7680x4320": "8K", "4096x2160": "4K", "3840x2160": "4K", "2560x1440": "1440p", "1920x1080": "HD", "1280x720": "720p",
};

/** The common name for a frame size (3840 x 2160 is 4K), or the size itself. */
export function formatFrameSize(width: number, height: number): string {
  return SIZES[`${width}x${height}`] ?? `${width} x ${height}`;
}

// The rates a mount keeps a recording at when it is within 0.2% of one (frame_rate in output/ffmpeg.py).
const STANDARD_RATES = [24000 / 1001, 24, 25, 30000 / 1001, 30, 48, 50, 60000 / 1001, 60];

/** A recording's rate as its mount keeps it, to two decimals: the nearest standard rate within 0.2%,
 * otherwise the rate itself. 24.002 is 24, 23.976 is 23.98 and 23.79 stays 23.79. */
export function mountedRate(fps: number): number {
  const nearest = STANDARD_RATES.reduce((best, rate) => Math.abs(fps - rate) < Math.abs(fps - best) ? rate : best);
  return Number((Math.abs(fps - nearest) / nearest < 0.002 ? nearest : fps).toFixed(2));
}

/** A frame rate as written on a camera: 24 fps, 23.98 fps. */
export function formatFps(fps: number): string {
  return `${Number(fps.toFixed(2))} fps`;
}
