<script lang="ts">
  import type { Schemas } from "../lib/api";
  import { timingPlot } from "../lib/diagnostic-plots";
  import AnalyticsChart from "./AnalyticsChart.svelte";
  import { channelColour } from "../lib/analytics-catalogue";
  import type { ChartLine } from "../lib/analytics-chart";
  import ReportActions from "./ReportActions.svelte";
  let { timing, name, zoomed = false }: { timing: Schemas["TimingDiagnostics"]; name: string; zoomed?: boolean } = $props();
  let groups = $state(false);
  let intervals = $derived(timingPlot(timing, zoomed));
  let lines: ChartLine[] = $derived([{ label: "Interval", colour: channelColour("Interval"), unit: "ms", samples: timing.buckets.map(bucket => {
    const frame = Math.floor((bucket.first_frame + bucket.last_frame) / 2) + 1;
    return { x: frame, frame, time: bucket.time_s, firstFrame: bucket.first_frame + 1, lastFrame: bucket.last_frame + 1,
      value: bucket.mean_ms, minimum: bucket.minimum_ms, maximum: bucket.maximum_ms };
  }) }]);
  const ms = (value: number | null) => value === null ? "Not available" : `${value.toFixed(4)} ms`;
</script>
<section aria-label="Frame timing result">
  <p>{timing.frames} frames · {timing.dropped} estimated missing · {timing.duplicated} short intervals · {timing.nonpositive} non-positive intervals</p>
  <dl><div><dt>Median interval</dt><dd>{ms(timing.median_ms)}</dd></div><div><dt>Shortest</dt><dd>{ms(timing.minimum_ms)}</dd></div><div><dt>Longest</dt><dd>{ms(timing.maximum_ms)}</dd></div>{#if timing.mean_ms != null}<div><dt>Mean interval</dt><dd>{ms(timing.mean_ms)}</dd></div>{/if}{#if timing.deviation_us != null}<div><dt>Standard deviation</dt><dd>{timing.deviation_us.toFixed(3)} µs</dd></div>{/if}</dl>
  {#if timing.intervals > 0}
    <label><input type="checkbox" role="switch" bind:checked={zoomed} /> Zoom timing variation</label>
    <AnalyticsChart label="Recorded frame intervals" {lines} xMin={2} xMax={Math.max(2, timing.frames)} yLabel="Interval (ms)" minimum={intervals.low} maximum={intervals.high} reference={timing.median_ms} highlightIntervals />
  {/if}
  <p>Every recorded interval contributes. At most 256 groups show minimum/maximum envelopes and mean intervals; the dashed line is the median. Diamonds mark long, short or non-positive group extrema. Missing frames are estimated from gaps over 1.5 times the median; short intervals are below half the median. This describes source timing before output frame-rate conversion.</p>
  <details ontoggle={(event) => groups = event.currentTarget.open}><summary>Interval groups (minimum, maximum and mean)</summary>{#if groups}<div class="table-scroll"><table><thead><tr><th>Recorded frames</th><th>Intervals</th><th>Min ms</th><th>Max ms</th><th>Mean ms</th></tr></thead><tbody>{#each timing.buckets as bucket}<tr><td>{bucket.first_frame + 1} to {bucket.last_frame + 1}</td><td>{bucket.intervals}</td><td>{bucket.minimum_ms}</td><td>{bucket.maximum_ms}</td><td>{bucket.mean_ms}</td></tr>{/each}</tbody></table></div>{/if}</details>
  <ReportActions value={{ clip: name, ...timing }} filename={`${name}.timing.json`} label="frame timing" />
</section>
<style>
  section { margin-top: 12px; min-width: 0; }
  p { color: var(--muted); line-height: 1.5; font-size: 11px; }
  label { font-size: 11px; color: var(--accent); }
  summary { cursor: pointer; font-size: 11px; }
  .table-scroll { overflow: auto; margin-top: 10px; max-height: 260px; }
  table { border-collapse: collapse; width: 100%; font-size: 10px; font-variant-numeric: tabular-nums; }
  th, td { padding: 6px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
  th:first-child { text-align: left; }
  dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(105px, 1fr)); gap: 8px; font-size: 11px; font-variant-numeric: tabular-nums; } dl > div { padding: 10px; border-radius: 6px; background: var(--bg); } dt { color: var(--muted); font-size: 10px; } dd { margin: 5px 0 0; }
</style>
