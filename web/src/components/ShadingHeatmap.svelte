<script lang="ts">
  import { heatmapColour, channelColour } from "../lib/analytics-catalogue";
  import HeatmapScale from "./HeatmapScale.svelte";
  import { onDestroy, untrack } from "svelte";
  import { api, type Schemas } from "../lib/api";
  import { flatField } from "../lib/flat-field";
  let { id, frames, step }: { id: string; frames: number[]; step: number } = $props();
  let selected = $state(0);
  let channel = $state(0);
  let view = $state<"gains" | "flat">("gains");
  let canvas = $state<HTMLCanvasElement | null>(null);
  let map = $state<Schemas["ShadingMap"] | null>(null);
  let message = $state("");
  let busy = $state(false);
  let request: AbortController | null = null;
  let revision = 0;
  const names = ["R", "Gr", "Gb", "B"];
  let plane = $derived(map?.channels[channel] ?? []);
  // Keep one colour scale across all four channels of this frame.
  let values = $derived(map?.channels.flat() ?? []);
  let low = $derived(values.length ? Math.min(...values) : 1);
  let high = $derived(values.length ? Math.max(...values) : 1);
  function cancel(): void { revision++; request?.abort(); request = null; busy = false; }
  async function load(frame: number): Promise<void> {
    cancel(); const asked = revision; request = new AbortController(); busy = true; map = null; message = "";
    const result = await api.shadingMap(id, frame, request.signal);
    if (asked !== revision) return;
    busy = false; request = null;
    if (result.ok) map = result.data; else message = result.message;
  }
  $effect(() => { const frame = frames[selected] ?? frames[0]; id; untrack(() => { if (frame !== undefined) void load(frame); }); });
  onDestroy(cancel);
  $effect(() => {
    const context = view === "flat" && map && canvas ? canvas.getContext("2d") : null;
    if (!context || !map) return;
    context.putImageData(new ImageData(flatField(map.channels, map.width, map.height) as Uint8ClampedArray<ArrayBuffer>, map.width, map.height), 0, 0);
  });
</script>
<section aria-label="Lens shading heatmap">
  <h4>Lens-shading heatmap</h4>
  <p>Recorded sensor coordinates; each cell is an actual map gain. Maps are available every {step} frames, plus the last frame when valid. Select a frame to read just that map.</p>
  <div class="controls"><label>View<select aria-label="Shading view" bind:value={view}><option value="gains">Channel gains</option><option value="flat">Flat field</option></select></label>
  {#if view === "gains"}<label>Channel<select aria-label="Shading channel" bind:value={channel}>{#each names as name, i}<option value={i}>{name}</option>{/each}</select></label>{/if}
  <label>Recorded frame<select aria-label="Shading frame" bind:value={selected}>{#each frames as frame, i}<option value={i}>{frame + 1}</option>{/each}</select></label></div>
  {#if busy}<p role="status">Reading shading map…</p>{/if}
  {#if message}<p role="status">{message}</p><button onclick={() => void load(frames[selected] ?? frames[0]!)}>Retry shading map</button>{/if}
  {#if map && view === "flat"}
    <div class="map-card"><div class="map-heading"><strong>Flat field · before correction</strong><span>Frame {map.frame + 1} · {map.width} × {map.height}</span></div>
    <canvas bind:this={canvas} width={map.width} height={map.height} aria-label={`Flat field of the lens shading at frame ${map.frame + 1}`}></canvas></div>
    <p>An evenly lit grey card as the sensor records it: darker where the lens vignettes, tinted where one colour falls off faster. The correction evens this out.</p>
  {:else if map}
    <div class="map-card"><div class="map-heading"><strong style:color={channelColour(names[channel]!)}>{names[channel]} · Sensor gain</strong><span>Frame {map.frame + 1} · {map.width} × {map.height}</span></div>
    <svg role="img" aria-label={`${names[channel]} lens shading map at frame ${map.frame + 1}`} viewBox={`0 0 ${map.width} ${map.height}`}>
      {#each plane as value, i}<rect x={i % map.width} y={Math.floor(i / map.width)} width="1" height="1" fill={heatmapColour(value, low, high)}><title>{names[channel]} · x {i % map.width}, y {Math.floor(i / map.width)}: {value.toPrecision(7)}×</title></rect>{/each}
    </svg>
    <HeatmapScale {low} {high} /></div>
    <p>Hover a cell for its exact gain. Colour scale is shared by all four channels in this frame.</p>
  {/if}
</section>
<style>
  h4, label { color: var(--accent); font-size: 12px; } p { color: var(--muted); font-size: 11px; line-height: 1.5; }
  label { display: grid; gap: 5px; margin: 8px 0; } select { color: var(--text); min-width: 0; }
  .controls { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
  .map-card { padding: 14px; margin-top: 12px; background: var(--bg); border: 1px solid var(--line); border-radius: 10px; }
  .map-heading { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 6px; font-size: 10px; } .map-heading span { color: var(--muted); }
  svg { display: block; width: 100%; max-height: 320px; margin-top: 14px; image-rendering: pixelated; }
  canvas { display: block; width: 100%; max-height: 320px; margin-top: 14px; object-fit: contain; }
</style>
