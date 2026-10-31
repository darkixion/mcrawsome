// Showing a frame as the stabilisation will: its kept frame (engine `resolve.kept_frame`) drawn to fill
// the picture, by one projective transform per frame.
import { writable } from "svelte/store";

/** Whether Develop shows the picture stabilised (kept for the page's lifetime, across clips). */
export const stabilisedView = writable(false);

type Point = readonly [number, number];

/** The projective map (3 x 3, row by row) taking each of `from`'s four points to the same one of `to`. */
export function homography(from: readonly Point[], to: readonly Point[]): number[] {
  const rows: number[][] = [];
  for (let i = 0; i < 4; i++) {
    const [x, y] = from[i]!, [u, v] = to[i]!;
    rows.push([x, y, 1, 0, 0, 0, -u * x, -u * y, u], [0, 0, 0, x, y, 1, -v * x, -v * y, v]);
  }
  for (let column = 0; column < 8; column++) {  // Gaussian elimination with partial pivoting
    let pivot = column;
    for (let row = column + 1; row < 8; row++) if (Math.abs(rows[row]![column]!) > Math.abs(rows[pivot]![column]!)) pivot = row;
    [rows[column], rows[pivot]] = [rows[pivot]!, rows[column]!];
    for (let row = 0; row < 8; row++) {
      if (row === column) continue;
      const factor = rows[row]![column]! / rows[column]![column]!;
      for (let k = column; k < 9; k++) rows[row]![k]! -= factor * rows[column]![k]!;
    }
  }
  return [...rows.map((row, i) => row[8]! / row[i]!), 1];
}

/** CSS that draws a picture of shape `ratio` (width / height), fitted into a `width` x `height` box as
 * `object-fit: contain` fits it, with frame corners `corners` (x, y fractions of the picture: top left,
 * top right, bottom right, bottom left) filling the picture; `clip` keeps it to the picture. */
export function keptTransform(corners: readonly number[], ratio: number, width: number, height: number): { transform: string; clip: string } {
  const wide = width / height > ratio;
  const w = wide ? height * ratio : width, h = wide ? height : width / ratio;
  const x = (width - w) / 2, y = (height - h) / 2;
  const quad: Point[] = [0, 2, 4, 6].map(i => [x + corners[i]! * w, y + corners[i + 1]! * h] as const);
  const picture: Point[] = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]];
  const [a, b, c, d, e, f, g, k, l] = homography(quad, picture) as [number, number, number, number, number, number, number, number, number];
  return { transform: `matrix3d(${a}, ${d}, 0, ${g}, ${b}, ${e}, 0, ${k}, 0, 0, 1, 0, ${c}, ${f}, 0, ${l})`, clip: `inset(${y}px ${x}px)` };
}
