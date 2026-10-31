// The clips chosen in the Library (their ids, in the Library's order), for bulk actions.
import { writable } from "svelte/store";

export const selection = writable<string[]>([]);
