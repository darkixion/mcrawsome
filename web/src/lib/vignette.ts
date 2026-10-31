import { api, developApi, type Schemas } from "./api";
import type { Applied } from "./bulk-adjustments";

export const PROFILE_LIMIT = 2 * 1024 * 1024;
export type Profile = Schemas["VignetteProfile"];
export type ProfileAction = "profile-export" | "profile-apply" | "profile-clear";
export type SelectionAction = "send" | "folder" | "mount-folder" | "mount" | "unmount" | "remove" | "trash" | ProfileAction;

export function parseProfile(value: unknown): Profile {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Choose a vignette profile JSON file");
  const p = value as Record<string, unknown>;
  const keys = new Set(["version", "sensor_width", "sensor_height", "cfa", "map_width", "map_height", "gains", "device_model", "camera_id"]);
  const integer = (v: unknown, min: number, max: number): v is number => typeof v === "number" && Number.isInteger(v) && v >= min && v <= max;
  if (Object.keys(p).some(key => !keys.has(key)) || (p.version !== undefined && p.version !== 1)
    || !integer(p.sensor_width, 1, 65536) || !integer(p.sensor_height, 1, 65536)
    || !["rggb", "bggr", "grbg", "gbrg"].includes(String(p.cfa))
    || !integer(p.map_width, 2, 128) || !integer(p.map_height, 2, 128)
    || !Array.isArray(p.gains) || p.gains.length !== 4) throw new Error("Invalid vignette profile dimensions or format");
  const count = p.map_width * p.map_height;
  if (p.gains.some(plane => !Array.isArray(plane) || plane.length !== count || plane.some(gain => typeof gain !== "number" || !Number.isFinite(gain) || gain <= 0 || gain > 256))
    || [p.device_model, p.camera_id].some(label => label !== undefined && (typeof label !== "string" || label.length > 256))) throw new Error("Invalid vignette profile gains or camera labels");
  return value as Profile;
}

export async function applyProfile(ids: string[], profile: Profile | null, progress: (result: Applied) => void): Promise<void> {
  for (const id of ids) {
    const current = await developApi.recipe(id);
    const saved = current.ok ? await api.saveVignetteProfile(id, current.data.layer.version, profile) : current;
    progress(saved.ok ? { id, ok: true } : { id, ok: false, message: saved.message });
  }
}
