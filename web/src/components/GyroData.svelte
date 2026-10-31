<script lang="ts">
  import { onDestroy } from "svelte";
  import { api, type Schemas } from "../lib/api";
  import AnalyticsChart from "./AnalyticsChart.svelte";
  import type { ChartLine } from "../lib/analytics-chart";
  import { gyroPaths, GYRO_COLOURS } from "../lib/gyro-plots";
  let { id, expanded = false, mode = null, onavailability }: { id: string; expanded?: boolean; mode?: "gyro" | "acceleration" | null; onavailability?: (acceleration: boolean) => void } = $props();
  let data = $state<Schemas["GyroPlot"] | null>(null);
  let busy = $state(false);
  let message = $state("");
  let acceleration = $state(false);
  $effect(() => { if (mode !== null) acceleration = mode === "acceleration"; });
  let axes = $state([true, true, true]);
  let request: AbortController | null = null;
  let revision = 0;
  let plot = $derived(data ? gyroPaths(acceleration ? data.acceleration ?? [] : data.angular_velocity, data.duration_s) : null);
  let buckets = $derived(data ? acceleration ? data.acceleration ?? [] : data.angular_velocity : []);
  let lines: ChartLine[] = $derived(["X", "Y", "Z"].map((axis, index) => ({ label: axis, colour: GYRO_COLOURS[index]!, unit: acceleration ? "m/s²" : "°/s", visible: axes[index],
    samples: buckets.map(bucket => ({ x: (bucket.start_s + bucket.end_s) / 2, value: bucket.mean[index] ?? null,
      minimum: bucket.minimum[index], maximum: bucket.maximum[index], frame: bucket.frame == null ? null : bucket.frame + 1 })) })));
  function cancel(): void { ++revision; request?.abort(); request = null; busy = false; }
  async function load(): Promise<void> {
    if (data || busy) return;
    cancel(); const asked = revision; request = new AbortController(); const signal = request.signal;
    busy = true; message = "";
    const result = await api.clipGyro(id, signal);
    if (asked !== revision || signal.aborted) return;
    busy = false; request = null;
    if (result.ok) { data = result.data; onavailability?.(data.acceleration !== null); } else message = result.message;
  }
  onDestroy(cancel);
</script>
<details class="gyro-data" open={expanded} ontoggle={(event) => { if (event.currentTarget.open) void load(); else cancel(); }}>
  <summary><strong>Gyro data</strong><span>Recorded angular velocity and acceleration</span></summary>
  {#if busy}<p role="status">Reading gyro samples…</p>{/if}
  {#if message}<p role="status">{message}</p><button onclick={() => void load()}>Retry reading gyro</button>{/if}
  {#if data && plot && (!acceleration || data.acceleration)}
    <p>{data.samples.toLocaleString()} samples · {data.duration_s.toFixed(2)} s{#if data.sample_rate_hz !== null} · {data.sample_rate_hz.toFixed(0)} Hz{/if}</p>
    {#if data.acceleration && mode === null}<label><input type="checkbox" role="switch" bind:checked={acceleration} /> Show acceleration</label>{/if}
    <div class="legend" aria-label="Visible sensor axes">{#each ["X", "Y", "Z"] as axis, index}<label style:color={GYRO_COLOURS[index]}><input type="checkbox" role="switch" bind:checked={axes[index]} /> {axis}</label>{/each}</div>
    {#key acceleration}<AnalyticsChart label={acceleration ? "Recorded acceleration" : "Recorded gyro angular velocity"} {lines}
      xMin={0} xMax={data.duration_s} xLabel="Time since first IMU sample" xUnit="seconds" yLabel={acceleration ? "Acceleration (m/s²)" : "Angular velocity (°/s)"}
      minimum={-plot.bound} maximum={plot.bound} reference={0} />{/key}
    {#if data.frame_note}<p class="muted">{data.frame_note}</p>{/if}
    <p class="muted">Recorded device axes, before camera mapping or stabilisation. Time starts at the first IMU sample. Lines show group means; shading preserves each group’s minimum and maximum.</p>
  {:else if data && acceleration && !data.acceleration}<p role="status">No acceleration samples were recorded for this clip.</p>{/if}
</details>
<style>
  .gyro-data { margin: 12px 0; min-width: 0; font-size: 12px; }
  summary { cursor: pointer; }
  summary span { display: block; margin: 5px 0 0 15px; color: var(--muted); font-size: 11px; }
  .legend { display: flex; gap: 8px; margin-top: 10px; } .legend label { padding: 5px 9px; background: var(--bg); border: 1px solid var(--line); border-radius: 5px; }
  label { font-size: 11px; }
  .muted { color: var(--muted); font-size: 11px; }
</style>
