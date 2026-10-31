// Hash routing: #/library, #/settings, #/about. Anything else, an old #/develop link included, is the Library.
import { readable } from "svelte/store";

export const SCREENS = ["library", "settings", "about"] as const;
export type Screen = (typeof SCREENS)[number];
export type Route = { screen: Screen };

export function parseRoute(hash: string): Route {
  const screen = hash.replace(/^#\/?/, "").split("/")[0];
  return { screen: SCREENS.find((name) => name === screen) ?? "library" };
}

export const route = readable<Route>(parseRoute(window.location.hash), (set) => {
  const changed = (): void => set(parseRoute(window.location.hash));
  window.addEventListener("hashchange", changed);
  return () => window.removeEventListener("hashchange", changed);
});
