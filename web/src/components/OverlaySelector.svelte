<script lang="ts">
  import { clipViews, setClipView } from "../lib/library";
  import { ANALYTICS_VIEWS } from "../lib/analytics-catalogue";
  import { onDestroy, untrack } from "svelte";
  import { api, type RecipeValues, type Schemas } from "../lib/api";
  import { overlayModel, type OverlayModel } from "../lib/preview-overlay";
  import type { IconName } from "./Icon.svelte";
  import ViewMenu from "./ViewMenu.svelte";
  let { id, recipe, raw, gyro, count, onmodel }: { id: string; recipe: RecipeValues; raw: boolean; gyro: boolean; count: number; onmodel: (value: OverlayModel | null) => void } = $props();
  let selected = $derived($clipViews[id]?.overlay ?? "off");
  let data = $state<Schemas["PreviewOverlays"] | null>(null);
  let message = $state("");
  let busy = $state(false);
  let request: AbortController | null = null;
  let revision = 0;
  const options = ANALYTICS_VIEWS;
  const ICONS: Record<string, IconName> = {
    gyro: "gyro", acceleration: "acceleration", blur: "shutter", timing: "clock", wb: "balance", shading: "vignette", noise_scale: "noise",
    noise_offset: "noise", iso: "levels", shutter: "shutter", focus: "focus", black: "black", white: "white",
  };
  let items = $derived([
    { value: "off", label: "No overlay", icon: "off" as IconName },
    ...options.filter(option => option.source === "video" || (option.source === "raw" ? raw : gyro)).map(option => {
      const missing = !!data && !data.charts.some(chart => chart.key === option.key);
      return { value: option.key as string, label: option.label, icon: ICONS[option.key] ?? "analytics", disabled: missing, title: missing ? "Not recorded in this clip" : undefined };
    }),
  ]);
  function choose(value: string) {
    setClipView(id, { overlay: value as Schemas["ClipView"]["overlay"] });
    if (selected === "off") cancel(); else if (!data && !busy) void load();
  }
  let settingsKey = $derived(JSON.stringify([id, recipe.output, recipe.camera]));
  let previous = "";
  function cancel() { ++revision; request?.abort(); request = null; busy = false; }
  async function load() {
    cancel(); const asked = revision; request = new AbortController(); const signal = request.signal;
    busy = true; message = "";
    const result = await api.previewOverlays(id, recipe, signal);
    if (asked !== revision || signal.aborted) return;
    busy = false; request = null;
    if (result.ok) data = result.data; else message = result.message;
  }
  $effect(() => {
    const key = settingsKey;
    untrack(() => { if (key === previous) return; previous = key; cancel(); data = null; message = ""; onmodel(null); if (selected !== "off") void load(); });
  });
  $effect(() => {
    const chart = selected === "off" ? null : data?.charts.find(chart => chart.key === selected);
    onmodel(chart && data?.frames === count ? overlayModel(chart, data.frames) : null);
  });
  onDestroy(() => { cancel(); onmodel(null); });
</script>

<div class="selector">
  <ViewMenu icon="overlay" label="Preview overlay" title="Overlay a recorded measurement on the preview (preview only). Faint lines show the whole clip; bright lines follow the displayed frame."
    shown={selected === "off" ? "" : options.find(option => option.key === selected)?.label ?? ""} value={selected} items={items} onselect={choose} />
  {#if selected !== "off"}
    {#if busy}<span class="status" role="status">Reading overlay…</span>
    {:else if message}<span class="status" role="status">{message}</span><button class="secondary retry" onclick={() => void load()}>Retry overlay</button>
    {:else if data && data.frames !== count}<span class="status" role="status">Reopen this preview to match the new frame timing.</span>
    {:else if data && !data.charts.some(chart => chart.key === selected)}<span class="status" role="status">{data.notes.join(" ") || "This measurement is unavailable."}</span>
    {:else if data}{@const help = data.charts.find(chart => chart.key === selected)?.help}{#if help}<span class="overlay-help" role="img" aria-label={`About this overlay: ${help}`} title={help}>ⓘ</span>{/if}{/if}
  {/if}
</div>
<style>
  .selector { display: flex; align-items: center; gap: 6px; min-width: 0; color: var(--muted); font-size: 10px; }
  .status { color: var(--muted); font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .retry { padding: 2px 6px; font-size: 10px; }
  .overlay-help { color: var(--muted); font-size: 12px; cursor: help; }
</style>
