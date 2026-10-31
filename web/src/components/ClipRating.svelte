<script lang="ts">
  import { annotationsFor } from "../lib/annotations";
  let { id, name, rating = 0 }: { id: string; name: string; rating?: number } = $props();
  let session = $derived(annotationsFor(id));
  let editor = $derived(session.state);
  $effect(() => { const current = session; const release = current.retain(); return () => { void current.flush(); release(); }; });
  let value = $derived($editor.values?.rating ?? rating);
  export async function rate(stars: number): Promise<void> {
    await session.rate(stars);
  }
</script>

<div class="rating" role="group" aria-label={`Rating for ${name}`}>
  {#each [0, 1, 2, 3, 4, 5] as stars}
    <button type="button" class:lit={stars > 0 && stars <= value} aria-label={stars === 0 ? "Unrated" : `${stars} ${stars === 1 ? "star" : "stars"}`} aria-pressed={value === stars} disabled={$editor.loading || $editor.saving}
      title={stars === 0 ? "Clear rating (0)" : `Rate ${stars} of 5 (${stars})`}
      onclick={(event) => { event.stopPropagation(); void rate(stars); }} onkeydown={(event) => event.stopPropagation()}>{stars === 0 ? "×" : "★"}</button>
  {/each}
</div>
{#if $editor.problem}<p class="error" role="alert">{$editor.problem.message}</p>{/if}

<style>
  .rating { display: flex; gap: 2px; margin-top: 8px; }
  button { background: transparent; color: var(--muted); padding: 2px 4px; min-width: 24px; border: 0; font-size: 16px; }
  button.lit { color: var(--accent); }
  button:focus-visible { outline: 1px solid var(--accent); }
  .error { color: var(--danger); font-size: 11px; }
</style>
