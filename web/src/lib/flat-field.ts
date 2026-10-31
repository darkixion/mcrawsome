// The picture an evenly lit grey card makes through the lens before correction: each sensor channel at the
// inverse of its shading gain, brightest at 1, with a display gamma so the falloff can be seen.
export function flatField(channels: number[][], width: number, height: number): Uint8ClampedArray {
  const [red, greenR, greenB, blue] = channels;
  const pixels = new Uint8ClampedArray(width * height * 4);
  if (!red || !greenR || !greenB || !blue) return pixels;
  const smallest = Math.min(...channels.flat().filter(gain => gain > 0));
  const shown = (gain: number) => 255 * Math.pow(gain > 0 ? Math.min(1, smallest / gain) : 0, 1 / 2.2);
  for (let i = 0; i < width * height; i++) {
    pixels[4 * i] = shown(red[i]!);
    pixels[4 * i + 1] = shown(2 / (1 / greenR[i]! + 1 / greenB[i]!));
    pixels[4 * i + 2] = shown(blue[i]!);
    pixels[4 * i + 3] = 255;
  }
  return pixels;
}
