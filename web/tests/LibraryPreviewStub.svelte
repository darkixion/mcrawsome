<script lang="ts">
  import type { Snippet } from "svelte";
  let { id, inspector, docked = false, ondock }: { id: string; inspector?: Snippet<[Snippet, string]>; docked?: boolean; ondock?: () => void; onflush?: unknown; onplayback?: unknown } = $props();
</script>
<section aria-label="Clip preview">Preview {id}{#if ondock}<button onclick={ondock}>{docked ? "Move the preview back" : "Move the preview to the clip list"}</button>{/if}</section>
{#snippet develop()}<p>Development controls {id}</p>{/snippet}
{#if inspector}{@render inspector(develop, "Saved")}{/if}
