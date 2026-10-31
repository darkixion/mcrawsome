<script lang="ts">
  import { ANALYTICS_VIEWS } from "../lib/analytics-catalogue";
  import { onDestroy, untrack } from "svelte";
  import { api, type Schemas } from "../lib/api";
  import CaptureCsv from "./CaptureCsv.svelte";
  import CaptureGraph from "./CaptureGraph.svelte";
  import ShadingHeatmap from "./ShadingHeatmap.svelte";
  import FrameTiming from "./FrameTiming.svelte";
  import GyroData from "./GyroData.svelte";
  import ReportActions from "./ReportActions.svelte";
  let { id, name, raw, gyro }: { id: string; name: string; raw: boolean; gyro: boolean } = $props();
  let data = $state<Schemas["CaptureAnalytics"] | null>(null);
  let busy = $state(false);
  let attempted = $state(false);
  let message = $state("");
  let selected = $state("timing");
  let hasAcceleration = $state<boolean | null>(null);
  let gyroRevision = $state(0);
  let request: AbortController | null = null;
  let revision = 0;
  let previous = "";
  let options = $derived([
    ...ANALYTICS_VIEWS.filter(view => view.source === "imu" ? gyro : view.key === "timing" ? !!data?.timing : data?.series.some(item => item.key === view.key || item.group === view.key))
      .map(view => ({ key: view.key as string, label: view.label as string, disabled: view.key === "acceleration" && hasAcceleration === false })),
    ...(data?.series.some(item => item.group === "levels") ? [{ key: "levels", label: "RAW black/white levels", disabled: false }] : []),
    ...(data?.shading_frames?.length ? [{ key: "heatmap", label: "Lens-shading heatmap", disabled: false }] : []),
  ]);
  let choice = $derived(options.find(item => item.key === selected) ?? options.find(item => !item.disabled));
  let series = $derived(data?.series.filter(item => item.key === choice?.key || item.group === choice?.key) ?? []);
  let imuView = $derived(choice?.key === "gyro" || choice?.key === "acceleration");
  let scan = $state<Schemas["CaptureScan"] | null>(null);
  let timer: ReturnType<typeof setTimeout> | null = null;
  function cancel(): void { ++revision; request?.abort(); request = null; if (timer) clearTimeout(timer); timer = null; busy = false; }
  async function poll(asked: number, refresh = false): Promise<void> {
    request = new AbortController(); const signal = request.signal;
    const result = await api.clipAnalytics(id, signal, refresh);
    if (asked !== revision || signal.aborted) return;
    request = null;
    if (!result.ok) { busy = false; message = result.message; attempted = true; return; }
    scan = result.data;
    if (scan.status === "ready" && scan.report) {
      data = scan.report; busy = false; attempted = true; message = "";
    } else if (scan.status === "failed") {
      busy = false; attempted = true; message = scan.message ?? "Could not read metadata";
    } else {
      busy = true; timer = setTimeout(() => { timer = null; void poll(asked); }, 750);
    }
  }
  function load(refresh = false): void {
    cancel(); if (refresh) { gyroRevision++; hasAcceleration = null; } busy = true; message = ""; scan = null; void poll(revision, refresh);
  }
  $effect(() => {
    const current = id;
    untrack(() => {
      if (previous === current) return;
      previous = current; cancel(); data = null; scan = null; attempted = false; message = ""; selected = "timing"; hasAcceleration = null;
      load();
    });
  });
  onDestroy(cancel);
</script>
<section class="analytics" aria-label="Clip analytics">
  <p>{raw ? "Recorded measurements before RAW overrides, stabilisation or output frame-rate conversion." : "Original LOG video and its recorded metadata; no mounting required."}</p>
  {#if busy}
    <p role="status">{scan?.status === "scanning" ? "Scanning frame metadata in the background…" : "Waiting for background metadata…"}
      {#if scan && scan.total > 0} {scan.completed} / {scan.total} frames{/if}
    </p>
    {#if scan && scan.total > 0}<progress aria-label="Metadata scan progress" value={scan.completed} max={scan.total}></progress>{:else}<progress aria-label="Metadata scan progress"></progress>{/if}
    <p>You can keep browsing and playing. This scan continues if you leave Analytics.</p>
  {:else}<button onclick={() => load(true)}>{data ? "Refresh capture analytics" : "Read capture analytics"}</button>{/if}
    {#if message}<p role="status">{message}</p>{/if}
    {#if data || (gyro && attempted && !busy)}
      {#if data && !data.timing}<p role="status">Frame timing unavailable: {data.timing_error}</p>{/if}
      {#each data?.notes ?? [] as note}<p>{note}</p>{/each}
      <section aria-label="Capture metadata graphs">
        <h3>Capture analytics</h3>
        {#if raw && data}<p>{data.sampled_frames} of {data.frames} records read. Every recorded frame included; the graph retains grouped extrema. Download CSV for individual frame values.</p>{/if}
        {#if data?.unreadable_frames}<p role="status">{data.unreadable_frames} frame records could not be read. Missing or invalid values leave gaps.</p>{/if}
        {#if choice}
          <label>Graph<select aria-label="Analytics graph" value={choice.key} onchange={(event) => selected = event.currentTarget.value}>{#each options as item (item.key)}<option value={item.key} disabled={item.disabled}>{item.label}{item.disabled ? " (not recorded)" : ""}</option>{/each}</select></label>
          {#if choice.key === "timing" && data?.timing}
            <FrameTiming zoomed timing={data.timing} {name} />
          {:else if choice.key === "heatmap"}
            {#key id}<ShadingHeatmap {id} frames={data?.shading_frames ?? []} step={data?.shading_step ?? 30} />{/key}
          {:else if !imuView}
            <CaptureGraph {series} frames={data?.frames ?? 0} title={choice.label} />
          {/if}
        {:else}<p>No per-frame capture coefficients were recorded in this file. Available timing and gyro data are shown separately.</p>{/if}
        {#if gyro && attempted && !busy}
          <div hidden={!imuView}>{#key `${id}:${gyroRevision}`}<GyroData {id} expanded={imuView} mode={choice?.key === "acceleration" ? "acceleration" : "gyro"} onavailability={(available) => hasAcceleration = available} />{/key}</div>
        {/if}
        {#if raw && data}<CaptureCsv {id} {name} disabled={busy} />{/if}
        {#if data}<ReportActions value={{ clip: name, ...data }} filename={`${name}.capture-analytics.json`} label="capture analytics" />{/if}
      </section>
    {/if}

  {#if !gyro}<p>No gyro log is available for this clip.</p>{/if}
</section>
<style>
  .analytics { min-width: 0; }
  p { color: var(--muted); font-size: 11px; line-height: 1.5; }
  h3, label { color: var(--accent); font-size: 12px; }
  label { display: grid; gap: 6px; }
  select { color: var(--text); min-width: 0; }
  button { font-size: 11px; }
  section[aria-label="Capture metadata graphs"] { margin-top: 16px; min-width: 0; }
</style>
