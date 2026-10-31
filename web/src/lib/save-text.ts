// Save a list the engine made (camera report or GPX) where the user chooses: through the desktop
// window's native dialog, or as a browser download.
import { get } from "svelte/store";
import { bridge } from "./bridge";

export async function saveText(content: string, name: string, type = "text/plain"): Promise<"saved" | "cancelled" | "downloaded"> {
  const native = get(bridge)?.api.save_text;
  if (native) return await native(content, name) ? "saved" : "cancelled";
  const url = URL.createObjectURL(new Blob([content], { type })), link = document.createElement("a");
  link.href = url; link.download = name; link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  return "downloaded";
}
