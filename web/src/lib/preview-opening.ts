import { developApi, type ClipRecipe, type RecipeValues, type Result } from "./api";
import { PreviewClient } from "./preview";

export function previewRecipe(saved: RecipeValues): RecipeValues {
  // Viewing a clip has one neutral display conversion at its effective RAW calibration.
  const calibration = Object.fromEntries(["rotation", "level_policy", "vignette_profile", "black_level", "white_level", "lens_shading", "lens_shading_mode", "input_encoding", "input_gamut", "input_transfer", "input_ei", "native_as"]
    .filter(field => saved.develop?.[field] != null)
    .map(field => [field, saved.develop![field]]));
  const crop = saved.develop?.mount_crop;
  if (typeof crop === "string" && crop !== "none") calibration.crop = crop;
  return { camera: saved.camera, output: saved.output,
    develop: { exposure: 0, ...calibration }, motion: { stabilise: "off" } };
}
function open(id: string): { id: string; client: PreviewClient; recipe: Promise<Result<ClipRecipe>> } {
  return { id, client: new PreviewClient(id), recipe: developApi.recipe(id) };
}
type Opening = ReturnType<typeof open>;
let warming: Opening | null = null;
let expiry: ReturnType<typeof setTimeout> | null = null;
let hover: { id: string; timer: ReturnType<typeof setTimeout> } | null = null;

export function leavePreview(id: string): void {
  if (hover?.id !== id) return;
  clearTimeout(hover.timer);
  hover = null;
}

export function hoverPreview(id: string): void {
  if (hover) leavePreview(hover.id);
  hover = { id, timer: setTimeout(() => warmPreview(id), 150) };
}

export function discardWarmPreview(): void {
  if (hover) leavePreview(hover.id);
  if (expiry !== null) clearTimeout(expiry);
  expiry = null;
  warming?.client.close();
  warming = null;
}

export function warmPreview(id: string): Opening {
  if (hover) leavePreview(hover.id);
  if (warming?.id !== id) {
    discardWarmPreview();
    warming = open(id);
  }
  if (expiry !== null) clearTimeout(expiry);
  expiry = setTimeout(discardWarmPreview, 10_000);
  return warming;
}

export function takePreview(id: string): Opening {
  const opening = warmPreview(id);
  if (expiry !== null) clearTimeout(expiry);
  expiry = null;
  warming = null;
  return opening;
}
