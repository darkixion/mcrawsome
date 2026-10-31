<script lang="ts">
  import { annotationsFor, type AnnotationValues } from "../lib/annotations";
  import { metadataHelp } from "../lib/metadata";
  import ErrorNote from "./ErrorNote.svelte";
  import ReportActions from "./ReportActions.svelte";
  let { id, name, onnext, focusOnLoad = false, place = null, placeKm = null }: { id: string; name: string; onnext?: () => void; focusOnLoad?: boolean; place?: string | null; placeKm?: number | null } = $props();
  // A city further than this is named with its distance, and not offered as the Location.
  const NEAR_KM = 10;
  const identity = $props.id();
  let form: HTMLFormElement;
  let title: HTMLInputElement;
  let focusAfter = $state<string | null>(null);
  let initiallyFocused = false;
  let session = $derived(annotationsFor(id));
  let editor = $derived(session.state);
  let values = $derived($editor.values);
  const fields = [
    { key: "scene", label: "Scene", limit: 256, placeholder: "12B" },
    { key: "shot", label: "Shot", limit: 256, placeholder: "C" },
    { key: "take", label: "Take", limit: 256, placeholder: "14A" },
    { key: "location", label: "Location", limit: 512, placeholder: "Bentley House Exterior" },
    { key: "camera", label: "Camera", limit: 256, placeholder: "A" },
  ] as const;
  $effect(() => { const current = session; const release = current.retain(); void current.load(); return () => { void current.flush(); release(); }; });
  $effect(() => {
    if (focusOnLoad && !initiallyFocused && values && !$editor.loading) {
      initiallyFocused = true;
      title?.focus({ preventScroll: true });
    }
    if (focusAfter !== null && focusAfter !== id && values && !$editor.loading) {
      focusAfter = null;
      title?.focus({ preventScroll: true });
      title?.scrollIntoView?.({ block: "nearest" });
    }
  });
  let status = $derived($editor.loading ? "Reading details..." : $editor.problem ? "Not saved" : $editor.saving ? "Saving..." : $editor.dirty ? "Unsaved changes" : $editor.record ? "Saved" : "");
  async function submit(): Promise<void> {
    const current = session;
    if (await current.flush() && current === session && onnext) { focusAfter = id; onnext(); }
  }
  function text(key: keyof AnnotationValues, event: Event): void { session.edit({ [key]: (event.currentTarget as HTMLInputElement).value }); }
</script>

<svelte:window onkeydown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && event.target instanceof Node && form?.contains(event.target)) { event.preventDefault(); void submit(); } }} />

<section class="notes" aria-label="Clip details editor">
  <header><div><h2>Clip details</h2><p>{name} · Editing this clip</p></div><span role="status" aria-label="Clip details save status">{status}</span></header>
  {#if $editor.problem}<ErrorNote error={$editor.problem} />{/if}
  <form bind:this={form!} onsubmit={(event) => { event.preventDefault(); void submit(); }}>
    <fieldset disabled={!values || $editor.loading}>
      <label title={metadataHelp("Title")}>Title<input bind:this={title!} type="text" maxlength="512" autocomplete="off" placeholder="A recognisable name for this clip" value={values?.title ?? ""} oninput={(event) => text("title", event)} /></label>
      <label title={metadataHelp("Description")}>Description<textarea rows="2" maxlength="16384" placeholder="What happens in this clip?" value={values?.description ?? ""} oninput={(event) => text("description", event)}></textarea></label>
      <label title={metadataHelp("Keywords")}>Keywords<input type="text" autocomplete="off" placeholder="exterior, arrival" aria-describedby={`${identity}-keywords-help`} value={$editor.keywordText} oninput={(event) => session.editKeywords(event.currentTarget.value)} /></label><p class="keyword-help" id={`${identity}-keywords-help`}>Separate keywords with commas.</p>
      <details><summary>Production details</summary>
        <div class="production">
          {#each fields as field (field.key)}<label title={metadataHelp(field.key === "camera" ? "Camera label" : field.label)}>{field.label}<input type="text" maxlength={field.limit} autocomplete="off" spellcheck={false} placeholder={field.placeholder} value={values?.[field.key] ?? ""} oninput={(event) => text(field.key, event)} /></label>{/each}
          {#if place && values && values.location !== place}{#if placeKm == null || placeKm <= NEAR_KM}<p class="place-hint">Recorded near {place} (the nearest city to its GPS position, found offline). <button type="button" class="quiet" onclick={() => session.edit({ location: place! })}>Use as Location</button></p>
          {:else}<p class="place-hint">Recorded about {Math.round(placeKm)} km from {place}, the nearest city the location data knows.</p>{/if}{/if}
          <label title={metadataHelp("Tag")}>Tag<select value={values?.tag ?? "untagged"} onchange={(event) => session.edit({ tag: event.currentTarget.value as AnnotationValues["tag"] })}><option value="untagged">Untagged</option><option value="good-take">Good Take</option><option value="rejected">Rejected</option></select></label>
        </div>
        <label title={metadataHelp("Notes")}>Notes<textarea rows="3" maxlength="16384" value={values?.notes ?? ""} oninput={(event) => text("notes", event)}></textarea></label>
      </details>
    </fieldset>
    <div class="save"><button type="submit" class="secondary" aria-label={onnext ? "Save details and next clip" : "Save details now"} title="Ctrl+Enter" disabled={!values || $editor.loading || (!$editor.dirty && !onnext) || $editor.saving || $editor.problem?.code === "recipe-changed"}>{onnext ? "Save & next" : "Save now"}</button>
      {#if $editor.problem}<button type="button" class="secondary" disabled={$editor.saving || $editor.loading} onclick={() => void session.load(true)}>Discard edits and reload</button>{/if}
      <span>Changes save automatically.</span>
    </div>
  </form>
  {#if $editor.record}<p class="storage">{$editor.record.message}</p>
    {#if !$editor.dirty && Object.keys(($editor.record.document.details ?? {}) as object).length}<ReportActions value={$editor.record.document} filename={`${name.replace(/\.[^.]+$/, "")}.meta`} label="clip details" />{/if}
  {/if}
  <p class="handoff">Send to Resolve includes saved details. Later edits apply to the next send.</p>
</section>

<style>
  .notes { scroll-margin-top: 76px; padding: 16px; margin-bottom: 16px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); }
  header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
  h2 { margin: 0; font-size: 15px; font-weight: 600; }
  header p, header span, .storage, .handoff, .save span { color: var(--muted); font-size: 11px; }
  header p { margin: 6px 0 0; overflow-wrap: anywhere; }
  header span { white-space: nowrap; }
  fieldset { margin: 0; padding: 0; border: 0; min-width: 0; }
  label { display: grid; gap: 6px; margin-bottom: 12px; font-size: 12px; }
  input[type="text"], textarea { width: 100%; min-width: 0; box-sizing: border-box; font: inherit; }
  textarea { resize: vertical; line-height: 1.5; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--text); }
  textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  details { border-top: 1px solid var(--line); padding-top: 12px; }
  summary { cursor: pointer; font-size: 12px; }
  .production { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 10px; margin-top: 14px; }
  .production label:nth-child(4) { grid-column: span 2; }
  .save { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .save button { font-size: 11px; }
  .secondary { background: transparent; color: var(--text); border: 1px solid var(--line); }
  .keyword-help { margin: -6px 0 12px; color: var(--muted); font-size: 11px; }
  .place-hint { grid-column: 1 / -1; margin: -4px 0 10px; color: var(--muted); font-size: 11px; }
  .place-hint button { font-size: 11px; padding: 1px 6px; }
  .storage, .handoff { margin: 10px 0 0; line-height: 1.5; }
  @media (max-width: 1200px) { .production { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
