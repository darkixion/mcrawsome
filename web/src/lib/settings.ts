// The interface's settings as last read from or saved to the engine, for the top bar and the screens.
import { writable } from "svelte/store";
import type { Schemas } from "./api";

export type InterfaceSettings = Schemas["SettingsView"];
export const settings = writable<InterfaceSettings | null>(null);
