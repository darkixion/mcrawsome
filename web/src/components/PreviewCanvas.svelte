<script lang="ts">
  import ScrubCanvas, { type Painter } from "./ScrubCanvas.svelte";
  let { placeholder = null, frame, index, scrubbing = false, native = false, onpainter }: { native?: boolean; placeholder?: string | null; scrubbing?: boolean; onpainter?: (painter: Painter | null) => void; index?: number; frame: string | null } = $props();
</script>
<div class="preview">
  <ScrubCanvas active={scrubbing || native} {onpainter} />
  {#if frame}<img src={frame} data-frame-index={index} data-native={native} alt="Clip preview" class:covered={scrubbing || native} />{:else}
    {#if placeholder}<img src={placeholder} alt="Library thumbnail" onerror={() => placeholder = null} />{:else}<p>Opening preview...</p>{/if}
  {/if}
</div>
<style>
  .preview { position: relative; width: 100%; height: 100%; display: grid; place-items: center; min-height: 0; background: #080b0d; }
  img { display: block; width: 100%; height: 100%; object-fit: contain; min-height: 0; }
  .covered { visibility: hidden; }
  p { color: var(--muted); }
</style>
