// Trim marks suggested from the gyro log: leave out the jolt of pressing record or stop and focus
// still settling. Suggestions are applied only when asked, and never over marks a clip already has.
import { api, developApi, type RecipeValues, type Schemas } from "./api";
import { secondsSyntax } from "./trim";

export type TrimSuggestion = Schemas["TrimSuggestion"];
export type SuggestedTrim = { id: string; ok: boolean; changed: boolean; message: string };

export function trimValues(trim: TrimSuggestion): NonNullable<RecipeValues["trim"]> {
  return { start: secondsSyntax(trim.start_s), duration: secondsSyntax(trim.end_s - trim.start_s) };
}

export function trimSummary(trim: TrimSuggestion | null | undefined, notes: string[] = []): string {
  if (trim?.reasons?.length) return trim.reasons.join(". ") + ".";
  return notes[0] ?? "No handling jolt or settling focus was found; the marks are unchanged.";
}

/** Suggest and save marks for each clip, keeping any clip's existing marks. */
export async function suggestTrims(ids: string[], progress: (result: SuggestedTrim) => void = () => {}): Promise<SuggestedTrim[]> {
  const results: SuggestedTrim[] = [];
  for (const id of ids) {
    let result: SuggestedTrim;
    const report = await api.motion(id);
    if (!report.ok) result = { id, ok: false, changed: false, message: report.message };
    else if (!report.data.trim?.reasons?.length) result = { id, ok: true, changed: false, message: trimSummary(report.data.trim, report.data.notes) };
    else {
      const current = await developApi.recipe(id);
      if (!current.ok) result = { id, ok: false, changed: false, message: current.message };
      else if (current.data.layer.values.trim?.start || current.data.layer.values.trim?.duration) result = { id, ok: true, changed: false, message: "Kept the marks this clip already has." };
      else {
        const values = { ...current.data.layer.values, trim: trimValues(report.data.trim) };
        const saved = await developApi.save(id, { version: current.data.layer.version, values });
        result = saved.ok ? { id, ok: true, changed: true, message: trimSummary(report.data.trim) } : { id, ok: false, changed: false, message: saved.message };
      }
    }
    results.push(result); progress(result);
  }
  return results;
}
