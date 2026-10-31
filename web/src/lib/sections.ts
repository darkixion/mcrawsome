// Which folding sections of the panel under the preview are open, remembered in this browser (a convenience).
import { writable } from "svelte/store";

const STORAGE = "mcrawsome.open-sections";

function restore(): Record<string, boolean> {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(STORAGE) ?? "{}");
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return {};
    return Object.fromEntries(Object.entries(saved).filter(([, open]) => typeof open === "boolean")) as Record<string, boolean>;
  } catch { return {}; }
}

export const openSections = writable<Record<string, boolean>>(restore());
openSections.subscribe((open) => { try { localStorage.setItem(STORAGE, JSON.stringify(open)); } catch { /* a convenience */ } });

export function setSectionOpen(id: string, open: boolean): void {
  openSections.update((all) => ({ ...all, [id]: open }));
}
