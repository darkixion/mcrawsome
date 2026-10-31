<script lang="ts">
  import type { Schemas } from "../lib/api";
  import AnalyticsChart from "./AnalyticsChart.svelte";
  import type { ChartLine } from "../lib/analytics-chart";
  let { series, frames, title }: { series: Schemas["CaptureSeries"][]; frames: number; title: string } = $props();
  import { channelColour } from "../lib/analytics-catalogue";
  let lines: ChartLine[] = $derived(series.map((item, index) => ({ label: item.label, colour: channelColour(item.label, index), unit: item.unit,
    samples: item.points.map(point => ({ x: point.frame + 1, frame: point.frame + 1, time: point.time_s,
      firstFrame: point.first_frame == null ? undefined : point.first_frame + 1,
      lastFrame: point.last_frame == null ? undefined : point.last_frame + 1,
      value: point.value, minimum: point.minimum, maximum: point.maximum })) })));
</script>
{#if series.length}
  <AnalyticsChart label={`Recorded ${title}`} {lines} xMin={1} xMax={frames} yLabel={series[0]?.unit ?? ""} />
  <p>Every valid frame contributes; up to 512 groups show mean and minimum/maximum. Groups containing missing data leave gaps.</p>
  {#each series as item}<p title={item.help}>{item.label}: {item.samples} valid frames.</p>{/each}
  <p>{series[0]?.help}</p>
{/if}
<style>
  p { color: var(--muted); font-size: 11px; line-height: 1.5; }
</style>
