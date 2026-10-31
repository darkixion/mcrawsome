import { api, developApi, type RecipeValues } from "./api";

const SECTIONS = new Set(["motion", "camera"]);
const DEVELOP = new Set(["rotation", "level_policy", "camera_model_source", "vignette_profile", "black_level", "white_level", "lens_shading", "lens_shading_mode", "input_encoding", "input_gamut", "input_transfer", "input_ei", "native_as", "mount_shading", "mount_proxy", "mount_first_frame", "mount_scale", "cfa_layout", "cfa_pattern", "mount_frame_rate", "mount_normalise_exposure", "mount_normalise_seconds", "mount_transfer", "mount_reduce_bits", "mount_crop", "mount_camera_model", "mount_unique_model"]);
export type Applied = { id: string; ok: boolean; message?: string };
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);

export function adjustmentValues(values: RecipeValues): RecipeValues {
  const result = Object.fromEntries(Object.entries(values).filter(([key]) => SECTIONS.has(key)));
  const develop = Object.fromEntries(Object.entries(values.develop ?? {}).filter(([key]) => DEVELOP.has(key)));
  if (Object.keys(develop).length) result.develop = develop;
  return result;
}
function merge(current: RecipeValues, patch: RecipeValues): RecipeValues {
  const result = { ...current };
  for (const [key, value] of Object.entries(patch)) result[key] = object(value) && object(current[key])
    ? merge(current[key] as RecipeValues, value) : structuredClone(value);
  return result;
}

export async function applyAdjustments(ids: string[], values: RecipeValues, progress: (result: Applied) => void = () => {}): Promise<Applied[]> {
  const patch = adjustmentValues(values), results: Applied[] = [];
  for (const id of ids) {
    const current = await developApi.recipe(id);
    let result: Applied;
    if (!current.ok) result = { id, ok: false, message: current.message };
    else {
      const saved = await developApi.save(id, { version: current.data.layer.version, values: merge(current.data.layer.values, patch) });
      result = saved.ok ? { id, ok: true } : { id, ok: false, message: saved.message };
    }
    results.push(result); progress(result);
  }
  return results;
}

export async function applyToAll(values: RecipeValues) {
  const current = await api.adjustmentDefaults();
  if (!current.ok) return current;
  return api.applyDefaults(current.data.version, adjustmentValues(values));
}
