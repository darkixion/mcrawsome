<script lang="ts">
  import { onDestroy, untrack } from "svelte";
  import { api, type Schemas } from "../lib/api";
  import { histogramPaths, PLANE_COLOURS } from "../lib/diagnostic-plots";
  import FrameTiming from "./FrameTiming.svelte";
  import ReportActions from "./ReportActions.svelte";
  let { id, name, playing = false }: { id: string; name: string; playing?: boolean } = $props();
  let open = $state(false);
  let busy = $state<"timing" | "histogram" | null>(null);
  let message = $state("");
  let histogram = $state<Schemas["RawHistogram"] | null>(null);
  let timing = $state<Schemas["TimingDiagnostics"] | null>(null);
  let logarithmic = $state(false);
  let request: AbortController | null = null;
  let revision = 0;
  let previous = "";
  let histPlot = $derived(histogram ? histogramPaths(histogram, logarithmic) : null);
  function cancel(notice = false): void {
    ++revision; request?.abort(); request = null;
    if (notice && busy) message = "Measurement cancelled";
    busy = null;
  }
  $effect(() => { if (id !== previous) untrack(() => { previous = id; cancel(); histogram = null; timing = null; message = ""; }); });
  $effect(() => { if (playing) untrack(() => cancel(true)); });
  onDestroy(() => cancel());
  async function measure(kind: "timing" | "histogram"): Promise<void> {
    if (playing || busy) return;
    cancel(); const asked = revision; request = new AbortController(); const signal = request.signal;
    busy = kind; message = "";
    const result = kind === "timing" ? await api.clipTiming(id, signal) : await api.rawHistogram(id, signal);
    if (asked !== revision || signal.aborted) return;
    request = null; busy = null;
    if (!result.ok) { message = result.message; return; }
    if (kind === "timing") timing = result.data as Schemas["TimingDiagnostics"];
    else histogram = result.data as Schemas["RawHistogram"];
  }
</script>
<details class="diagnostics" ontoggle={(event) => { open = event.currentTarget.open; if (!open) cancel(true); }}>
  <summary><strong>RAW diagnostics</strong><span>Recorded frame timing and first-frame photosites</span></summary>
  {#if open}
    <p>Read frame timing from the recording index, or measure every photosite in the first recorded RAW frame.</p>
    <div class="tools"><button disabled={playing || busy !== null} onclick={() => void measure("timing")}>Read frame timing</button><button disabled={playing || busy !== null} onclick={() => void measure("histogram")}>Measure first RAW frame</button>{#if busy}<button class="secondary" onclick={() => cancel(true)}>Cancel measurement</button>{/if}</div>
    {#if playing}<p role="status">Pause playback to measure RAW data.</p>{/if}
    {#if busy}<p role="status">{busy === "timing" ? "Reading recorded frame intervals..." : "Measuring the first recorded RAW frame..."}</p>{/if}
    {#if message}<p role="status">{message}</p>{/if}
    {#if timing}
      <FrameTiming {timing} {name} />
    {/if}
    {#if histogram && histPlot}
      <section aria-label="RAW histogram result">
        <h4>First-frame RAW histogram</h4>
        <p>{histogram.width} x {histogram.height} · {histogram.cfa.toUpperCase()} · recorded frame 1. Counts before black subtraction, lens shading, white balance or exposure.</p>
        <label><input type="checkbox" role="switch" bind:checked={logarithmic} /> Log counts</label>
        <svg role="img" aria-label="First-frame RAW histogram" viewBox="0 0 320 145">
          <path d="M30,10 V110 H290" stroke="currentColor" fill="none" /><text x="30" y="8">{histPlot.peak} / bin{logarithmic ? " (log scale)" : ""}</text><text x="30" y="128">0</text><text x="270" y="128">{histogram.bin_edges.at(-1)}</text><text x="95" y="143">Recorded RAW code value</text>
          {#each histPlot.paths as path, index}<path d={path} fill="none" stroke={PLANE_COLOURS[index]} stroke-width="1.5" />{/each}
        </svg>
        <div class="legend">{#each histogram.planes as plane, index}<span style:color={PLANE_COLOURS[index]}>{plane.name}</span>{/each}</div>
        <div class="table-scroll"><table><thead><tr><th>Plane</th><th>Samples</th><th>Min / Max</th><th>Black / White</th><th>At/below black</th><th>At/above white</th></tr></thead><tbody>{#each histogram.planes as plane}<tr><th>{plane.name}</th><td>{plane.samples}</td><td>{plane.minimum} / {plane.maximum}</td><td>{plane.black_level} / {plane.white_level}</td><td>{plane.at_or_below_black} ({(plane.at_or_below_black / plane.samples * 100).toFixed(3)}%)</td><td>{plane.at_or_above_white} ({(plane.at_or_above_white / plane.samples * 100).toFixed(3)}%)</td></tr>{/each}</tbody></table></div>
        <p>Each photosite is counted once. Threshold counts use the exact recorded levels and are independent of the 256 display bins. Above-white codes remain included. This is recorded sensor data, not the displayed image or Recipe overrides.</p>
        <ReportActions value={{ clip: name, ...histogram }} filename={`${name}.raw-histogram.json`} label="RAW histogram" />
      </section>
    {/if}
  {/if}
</details>
<style>
  .diagnostics { margin-top: 10px; padding: 14px; border: 1px solid var(--line); border-radius: 8px; font-size: 12px; }
  summary { cursor: pointer; } summary span { display: block; margin: 5px 0 0 15px; color: var(--muted); font-size: 11px; }
  p { color: var(--muted); line-height: 1.5; font-size: 11px; } h4 { margin: 16px 0 8px; }
  .tools { display: flex; flex-wrap: wrap; gap: 8px; } button { font-size: 11px; padding: 7px 10px; } button:disabled { opacity: .5; }
  .secondary { background: transparent; border: 1px solid var(--line); color: var(--text); }
  section { margin-top: 14px; padding-top: 4px; border-top: 1px solid var(--line); }
  svg { width: 100%; max-height: 260px; color: var(--muted); margin-top: 10px; } text { fill: currentColor; font-size: 9px; }
  .legend { display: flex; flex-wrap: wrap; gap: 12px; justify-content: center; font-size: 11px; margin: 10px 0; }
  .table-scroll { overflow: auto; margin-top: 10px; max-height: 260px; } table { border-collapse: collapse; width: 100%; font-size: 10px; font-variant-numeric: tabular-nums; }
  th, td { padding: 6px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; } th:first-child { text-align: left; }
</style>
