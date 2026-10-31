// Pick the higher WCAG contrast against an opaque CSS colour. The badge fill comes from the theme.
export function contrastText(colour: string): "#000" | "#fff" | null {
  const hex = /^#([\da-f]{6})$/i.exec(colour.trim());
  const rgb = /^rgba?\(\s*([\d.]+)[, ]+([\d.]+)[, ]+([\d.]+)(?:\s*[,/]\s*([\d.]+))?\s*\)$/i.exec(colour.trim());
  const channels = hex ? [0, 2, 4].map(offset => parseInt(hex[1]!.slice(offset, offset + 2), 16))
    : rgb && (rgb[4] === undefined || Number(rgb[4]) === 1) ? rgb.slice(1, 4).map(Number) : null;
  if (!channels || channels.some(value => !Number.isFinite(value) || value < 0 || value > 255)) return null;
  const linear = channels.map(value => { const s = value / 255; return s <= .04045 ? s / 12.92 : ((s + .055) / 1.055) ** 2.4; });
  const luminance = .2126 * linear[0]! + .7152 * linear[1]! + .0722 * linear[2]!;
  return (luminance + .05) / .05 >= 1.05 / (luminance + .05) ? "#000" : "#fff";
}
