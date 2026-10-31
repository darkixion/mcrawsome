<script lang="ts">
  import { selection } from "../lib/selection";
  import { clips, openedClips } from "../lib/library";
  import type { RecipeValues } from "../lib/api";
  import { adjustmentValues, applyAdjustments, applyToAll, type Applied } from "../lib/bulk-adjustments";
  import { suggestTrims, type SuggestedTrim } from "../lib/suggested-trims";
  import InfoTip from "./InfoTip.svelte";
  // The row of actions above the Resolve settings: Suggest trim, Apply to Selected, Set as Default and Reset,
  // and for several selected clips with gyro logs, Suggest trims.
  let { id, values, onreset, onsuggesttrim, suggestingTrim = false, trimNote = "" }: {
    id: string; values: RecipeValues; onreset?: () => void;
    onsuggesttrim?: () => void; suggestingTrim?: boolean; trimNote?: string; // this clip's marks from its gyro log
  } = $props();
  let chosen = $derived([...new Map([...($clips ?? []), ...$openedClips].map(clip => [clip.id, clip])).values()].filter(clip => clip.id !== null && clip.status === "ready" && $selection.includes(clip.id)));
  let others = $derived(chosen.filter(clip => clip.id !== id));
  let adjustments = $derived(adjustmentValues(values));
  let busy = $state(false);
  let results = $state<Applied[]>([]);
  let snapshotIds = $state<string[]>([]);
  let processed = $state(0);
  let allMessage = $state("");
  let batchKey = $state("");
  async function apply() {
    if (busy) return;
    const completed = new Set(results.filter(result => result.ok).map(result => result.id));
    const ids = others.map(clip => clip.id!).filter(id => !completed.has(id));
    snapshotIds = ids; processed = 0; busy = true;
    results = results.filter(result => result.ok);
    try { await applyAdjustments(ids, $state.snapshot(adjustments), result => { processed += 1; results = [...results, result]; }); }
    finally { busy = false; }
  }
  let trims = $state<SuggestedTrim[]>([]);
  let trimming = $state(false);
  async function suggest(): Promise<void> {
    if (trimming) return;
    trimming = true; trims = [];
    try { await suggestTrims(chosen.filter(clip => clip.gyro).map(clip => clip.id!), result => { trims = [...trims, result]; }); }
    finally { trimming = false; }
  }
  async function applyAll(): Promise<void> {
    if (busy) return;
    busy = true; allMessage = "";
    try {
      const result = await applyToAll($state.snapshot(adjustments));
      allMessage = result.ok ? "Defaults updated. Clips and folders with their own settings keep them." : result.message;
    } finally { busy = false; }
  }
  $effect(() => {
    const key = JSON.stringify(adjustments);
    if (!busy && key !== batchKey) { batchKey = key; results = []; }
  });
</script>

<section class="selected" aria-label="Develop selected clips">
  <div class="apply">
    {#if onsuggesttrim && !(chosen.length > 1 && chosen.some(clip => clip.gyro))}<button class="secondary" disabled={suggestingTrim} onclick={onsuggesttrim} title="Set the In and Out marks from the gyro log: past the jolt of pressing record, before the jolt of pressing stop, and after focus settles">{suggestingTrim ? "Suggesting..." : "Suggest trim"}</button>{/if}
    <!-- The clip shown already has these settings; the count is every selected clip, so it matches the selection. -->
    {#if others.length > 0}<button disabled={busy || Object.keys(adjustments).length === 0} onclick={() => void apply()}>{busy ? "Applying adjustments..." : results.some(result => !result.ok) ? "Retry failed clips" : `Apply to Selected (${chosen.length} clips)`}</button>
      <InfoTip label="Apply to Selected" text="Gives every selected clip this clip's stabilisation, orientation, camera and RAW settings. Only the selected clips change. Trim and output settings are kept." />{/if}
    <button class="secondary default" disabled={busy || Object.keys(adjustments).length === 0} onclick={() => void applyAll()}>Set as Default</button>
    <InfoTip label="Set as Default" text="Makes this clip's stabilisation, orientation, camera and RAW settings the defaults for all clips, selected or not. Clips and folders that have their own settings keep them; every other clip, including clips added later, uses the new defaults. Trim and output settings are kept." />
    {#if onreset}<button class="secondary" onclick={onreset}>Reset</button>{/if}
  </div>
  {#if chosen.length > 1 && chosen.some(clip => clip.gyro)}<div class="apply">
    <button class="secondary" disabled={trimming} onclick={() => void suggest()}>{trimming ? "Suggesting trims..." : `Suggest trims (${chosen.filter(clip => clip.gyro).length} clips)`}</button>
    <InfoTip label="Suggest trims" text="Sets each selected clip's In and Out marks from its gyro log: past the jolt of pressing record, before the jolt of pressing stop, and after focus settles. Clips without a gyro log, or with marks already, are left as they are." />
  </div>{/if}
  {#if trimNote}<p role="status">{trimNote}</p>{/if}
  {#if trims.length > 0}<p role="status">{trims.filter(item => item.changed).length} trimmed · {trims.filter(item => item.ok && !item.changed).length} unchanged{#if trimming} · {trims.length} / {chosen.filter(clip => clip.gyro).length} processed{/if}</p>{/if}
  {#each trims.filter(item => !item.ok) as item (item.id)}<p role="alert">{chosen.find(clip => clip.id === item.id)?.name ?? item.id}: {item.message}</p>{/each}
  {#if allMessage}<p role="status">{allMessage}</p>{/if}
  {#if results.length > 0}<p role="status">{results.filter(result => result.ok).length} applied{#if busy} · {processed} / {snapshotIds.length} processed{/if}</p>{/if}
  {#each results.filter(result => !result.ok) as result (result.id)}<p role="alert">{chosen.find(clip => clip.id === result.id)?.name ?? result.id}: {result.message}</p>{/each}
</section>

<style>
  .selected { grid-column: 1 / -1; margin: 0 0 10px; }
  .apply { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 4px 6px; }
  .apply + .apply { margin-top: 8px; }
  .apply button { font-size: 12px; }
  p { color: var(--muted); font-size: 11px; margin: 8px 0 0; }
  button:disabled { opacity: .5; cursor: not-allowed; }
</style>
