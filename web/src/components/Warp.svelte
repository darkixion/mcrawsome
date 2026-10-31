<script lang="ts">
  import type { Snippet } from "svelte";
  import { keptTransform } from "../lib/warp";
  // The picture (and what is drawn over it in step with it) shown as the stabilisation will: `corners`,
  // this frame's kept frame, fills the picture. Without them it is shown as it is, and nothing is measured.
  let { corners = null, width, height, children }: { corners?: readonly number[] | null; width: number; height: number; children: Snippet } = $props();
  let box: HTMLDivElement;
  let size = $state({ width: 0, height: 0 });
  let active = $derived(corners !== null);
  $effect(() => {
    if (!active) return;
    const measure = () => { size = { width: box.clientWidth, height: box.clientHeight }; };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    return () => observer.disconnect();
  });
  let placed = $derived(corners && size.width > 0 && size.height > 0 ? keptTransform(corners, width / height, size.width, size.height) : null);
</script>
<div class="warp" bind:this={box!} style:clip-path={placed?.clip} data-stabilised={placed !== null}>
  <div class="content" style:transform={placed?.transform}>{@render children()}</div>
</div>
<style>
  .warp { position: absolute; inset: 0; overflow: hidden; }
  .content { position: absolute; inset: 0; transform-origin: 0 0; }
</style>
