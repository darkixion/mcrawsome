// Pictures through each clip for skimming its card, asked of the engine one clip at a time: it makes a
// skim's frames in turn, so a sweep across the cards asks only for the one the pointer rested on last.
import { get, writable } from "svelte/store";
import { api } from "./api";

const LIMIT = 24; // clips remembered, most recently made last
export const skims = writable<Record<string, string[]>>({});
export const skimKey = (id: string, revision = 0): string => `${id}:${revision}`;
let asking = false;
let next: { key: string; id: string } | null = null;

/** Make `id`'s skim (at its thumbnail `revision`) once the engine has finished the one it is making. */
export function wantSkim(id: string, revision = 0): void {
  const key = skimKey(id, revision);
  if (key in get(skims)) return;
  next = { key, id };
  if (!asking) void ask();
}

async function ask(): Promise<void> {
  asking = true;
  try {
    while (next) {
      const { key, id } = next;
      next = null;
      if (key in get(skims)) continue;
      const result = await api.skim(id);
      if (!result.ok) continue; // asked again when the pointer next rests on the card
      skims.update(made => Object.fromEntries([
        ...Object.entries(made).filter(([kept]) => kept !== key),
        [key, result.data.frames.map(frame => `data:image/jpeg;base64,${frame}`)],
      ].slice(-LIMIT)));
    }
  } finally { asking = false; }
}

export function forgetSkims(): void {
  skims.set({});
  next = null;
}
