// Each clip owns its draft and save queue, so switching previews cannot redirect an in-flight edit.
import { get, writable } from "svelte/store";
import { api, type ApiError, type Schemas } from "./api";

export type AnnotationValues = Required<Schemas["ClipAnnotations"]>;
type Record = Schemas["AnnotationRecord"];
export type AnnotationState = {
  record: Record | null; values: AnnotationValues | null; keywordText: string;
  loading: boolean; saving: boolean; dirty: boolean; problem: ApiError | null;
};
const defaults = (): AnnotationValues => ({ title: "", description: "", keywords: [], scene: "", shot: "", take: "", location: "", tag: "untagged", notes: "", camera: "", rating: 0 });

export const annotationRatings = writable<{ [id: string]: number }>({});
/** The words a Library search matches, kept current while details are edited (as the engine joins them). */
export const annotationTexts = writable<{ [id: string]: string }>({});
export function searchText(values: AnnotationValues): string {
  return [values.title, values.description, ...values.keywords, values.notes, values.scene, values.shot,
    values.take, values.location, values.camera].filter(Boolean).join("\n");
}

export class AnnotationSession {
  readonly state = writable<AnnotationState>({ record: null, values: null, keywordText: "", loading: false, saving: false, dirty: false, problem: null });
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pending: Promise<boolean> | null = null;
  private revision = 0;
  private request = 0;
  private readers = 0;
  constructor(readonly id: string) {}

  get retained(): boolean { return this.readers > 0; }
  retain(): () => void {
    this.readers += 1;
    let active = true;
    return () => { if (active) { active = false; this.readers -= 1; } };
  }

  async load(discard = false): Promise<void> {
    const state = get(this.state);
    if (state.loading || state.saving || (!discard && state.dirty)) return;
    this.cancelTimer();
    const asked = ++this.request;
    this.state.update(state => ({ ...state, loading: true }));
    const result = await api.annotations(this.id);
    if (asked !== this.request) return;
    if (!result.ok) { this.state.update(state => ({ ...state, loading: false, problem: result })); return; }
    const values = { ...defaults(), ...result.data.values };
    annotationRatings.update(ratings => ({ ...ratings, [this.id]: values.rating }));
    annotationTexts.update(texts => ({ ...texts, [this.id]: searchText(values) }));
    this.state.set({ record: result.data, values, keywordText: values.keywords.join(", "), loading: false, saving: false, dirty: false, problem: null });
  }

  edit(patch: Partial<AnnotationValues>): void {
    const state = get(this.state);
    if (!state.values || state.loading) return;
    this.revision += 1;
    if (patch.rating !== undefined) annotationRatings.update(ratings => ({ ...ratings, [this.id]: patch.rating! }));
    const conflict = state.problem?.code === "recipe-changed";
    annotationTexts.update(texts => ({ ...texts, [this.id]: searchText({ ...state.values!, ...patch }) }));
    this.state.update(state => ({ ...state, values: { ...state.values!, ...patch }, dirty: true, problem: conflict ? state.problem : null }));
    this.cancelTimer();
    if (!conflict) this.timer = setTimeout(() => { this.timer = null; void this.flush(); }, 500);
  }

  async rate(rating: number): Promise<void> {
    if (get(this.state).loading) await new Promise<void>(resolve => {
      let unsubscribe = () => {};
      unsubscribe = this.state.subscribe(state => { if (!state.loading) { unsubscribe(); resolve(); } });
    });
    if (!get(this.state).values) await this.load();
    this.edit({ rating });
    await this.flush();
  }

  editKeywords(text: string): void {
    this.edit({ keywords: [...new Set(text.split(",").map(word => word.trim()).filter(Boolean))] });
    this.state.update(state => ({ ...state, keywordText: text }));
  }

  async flush(): Promise<boolean> {
    this.cancelTimer();
    if (this.pending) return this.pending;
    this.pending = this.save();
    try { return await this.pending; }
    finally { this.pending = null; }
  }

  private async save(): Promise<boolean> {
    for (;;) {
      const state = get(this.state);
      if (!state.dirty) return true;
      if (!state.record || !state.values || state.problem?.code === "recipe-changed") return false;
      const revision = this.revision;
      const values = structuredClone(state.values);
      this.state.update(state => ({ ...state, saving: true, problem: null }));
      const result = await api.saveAnnotations(this.id, state.record.version, values);
      if (!result.ok) { this.state.update(state => ({ ...state, saving: false, problem: result })); return false; }
      this.state.update(state => ({ ...state, record: result.data, saving: false, dirty: this.revision !== revision,
        values: this.revision === revision ? { ...defaults(), ...result.data.values } : state.values }));
      if (this.revision === revision) return true;
      this.cancelTimer(); // include newer edits in this serial queue
    }
  }

  private cancelTimer(): void { if (this.timer) clearTimeout(this.timer); this.timer = null; }
  dispose(): void { this.cancelTimer(); this.request += 1; }
}

const sessions = new Map<string, AnnotationSession>();
export function annotationsFor(id: string): AnnotationSession {
  let session = sessions.get(id);
  if (!session) { session = new AnnotationSession(id); sessions.set(id, session); }
  if (sessions.size > 64) for (const [key, item] of sessions) {
    const state = get(item.state);
    if (key !== id && !item.retained && !state.dirty && !state.loading && !state.saving) { item.dispose(); sessions.delete(key); break; }
  }
  return session;
}
export const hasUnsavedAnnotations = (): boolean => [...sessions.values()].some(item => get(item.state).dirty);
export function clearAnnotationSessions(): void { annotationRatings.set({}); annotationTexts.set({}); for (const session of sessions.values()) session.dispose(); sessions.clear(); }

export async function flushAnnotations(): Promise<boolean> {
  for (const session of sessions.values()) if (!await session.flush()) return false;
  return true;
}
