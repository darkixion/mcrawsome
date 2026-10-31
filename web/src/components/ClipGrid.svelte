<script lang="ts">
  import type { Schemas } from "../lib/api";
  import { clipKey, type ClipSummary } from "../lib/library";
  import ClipCard from "./ClipCard.svelte";

  let { clips, selected, active = null, contextId = null, onchoose, oncontext, clipMounts = () => [], isMounting = () => false, prefetch = true }: {
    isMounting?: (clip: ClipSummary) => boolean;
    contextId?: string | null; active?: string | null; clips: ClipSummary[]; selected: string[]; prefetch?: boolean;
    clipMounts?: (clip: ClipSummary) => Schemas["MountedSequence"][];
    onchoose: (clip: ClipSummary, event: MouseEvent | KeyboardEvent) => void;
    oncontext?: (clip: ClipSummary, event: MouseEvent | KeyboardEvent) => void;
  } = $props();
  let chosen = $derived(new Set(selected));
</script>

<div class="grid" role="listbox" aria-label="Clips" aria-multiselectable="true">
  {#each clips as clip, position (clipKey(clip))}
    <ClipCard menuOpen={clip.id !== null && contextId === clip.id} active={clip.id !== null && clip.id === active} {clip} {prefetch} mounts={clipMounts(clip)} mounting={isMounting(clip)} position={position + 1} oncontext={(event) => oncontext?.(clip, event)} selected={clip.id !== null && chosen.has(clip.id)} onchoose={(event) => onchoose(clip, event)} />
  {/each}
</div>

<style>
  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 12px;
  }
</style>
