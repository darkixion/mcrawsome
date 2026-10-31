import { writable } from "svelte/store";

export const SESSION_SUFFIX = ".mcrawsome-session.json";

/** A session file opened from the desktop, waiting for the session bar, with the recordings opened
 * alongside it, which join the session once it has loaded. */
export type SessionRequest = { path: string; paths: string[] };
export const sessionRequest = writable<SessionRequest | null>(null);

/** Desktop file types match names regardless of case, so this does too. */
export function isSession(path: string): boolean {
  return path.toLowerCase().endsWith(SESSION_SUFFIX);
}
