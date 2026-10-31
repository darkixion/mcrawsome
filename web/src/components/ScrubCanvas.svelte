<script module lang="ts">
  import type { Header } from "../lib/preview";
  export type Painter = (image: HTMLImageElement | undefined, header: Header) => void;
</script>
<script lang="ts">
  import { onMount } from "svelte";
  let { active = false, onpainter = () => {} }: { active?: boolean; onpainter?: (painter: Painter | null) => void } = $props();
  let canvas: HTMLCanvasElement;
  let draws = 0;
  $effect(() => { if (!active && canvas) canvas.style.display = "none"; });
  onMount(() => {
    const context = canvas.getContext("2d");
    if (!context) return;
    onpainter((image, header) => {
      if (!image) return;
      const sourceWidth = image.naturalWidth || header.width || 1;
      const sourceHeight = image.naturalHeight || header.height || 1;
      const width = Math.min(640, sourceWidth);
      const height = Math.max(1, Math.round(sourceHeight * width / sourceWidth));
      if (canvas.width !== width) canvas.width = width;
      if (canvas.height !== height) canvas.height = height;
      context.drawImage(image, 0, 0, width, height);
      canvas.dataset.frameIndex = String(header.index);
      canvas.dataset.draw = String(++draws);
      canvas.dataset.frameUrl = image.src;
      canvas.style.display = "block";
    });
    return () => onpainter(null);
  });
</script>
<canvas bind:this={canvas!} aria-label="Scrub preview" style="display: none"></canvas>
<style>
  canvas { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; background: #080b0d; pointer-events: none; }
</style>
