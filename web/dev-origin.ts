export function proxyOrigin(origin: string | undefined, own: string[], engine: string): string | undefined {
  return origin && own.some((url) => new URL(url).origin === origin) ? engine : origin;
}
