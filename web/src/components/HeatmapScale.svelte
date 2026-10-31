<script lang="ts">
  import { HEATMAP_STOPS } from "../lib/analytics-catalogue";
  import { chartNumber } from "../lib/analytics-chart";
  let { low, high }: { low: number; high: number } = $props();
</script>
<div class="gain-scale" aria-label={`Shared gain scale: ${chartNumber(low)} to ${chartNumber(high)} times`}>
  <div class="scale-title"><span>GAIN · ALL CHANNELS</span><span>Stronger correction →</span></div>
  <div class="ramp" style:background={low === high ? HEATMAP_STOPS[2] : `linear-gradient(90deg, ${HEATMAP_STOPS.join(",")})`}></div>
  <div class="numbers"><span>{chartNumber(low)}×</span><span>{chartNumber((low + high) / 2)}×</span><span>{chartNumber(high)}×</span></div>
</div>
<style>
  .gain-scale { margin-top: 12px; font-variant-numeric: tabular-nums; }
  .scale-title, .numbers { display: flex; justify-content: space-between; gap: 10px; color: var(--muted); font-size: 9px; }
  .scale-title > span:first-child { letter-spacing: .08em; }
  .ramp { height: 8px; margin: 7px 0 5px; border-radius: 3px; border: 1px solid #ffffff12; }
</style>
