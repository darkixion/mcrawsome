<script lang="ts">
  // What the engine found about a clip before it is developed: gyro, dropped frames, recovery, profile,
  // and capture warnings the recording's own data explains (read in the background, no picture decoded).
  import { untrack } from "svelte";
  import { api, type Schemas } from "../lib/api";
  import type { ClipDetails } from "../lib/library";

  let { clip }: { clip: ClipDetails } = $props();
  type Row = { name: string; value: string; tone: "ok" | "warn" | "plain" };
  let checks = $state<{ id: string | null; items: Schemas["CaptureCheck"][] }>({ id: null, items: [] });
  // Read once per clip: a refreshed copy of the same clip keeps its checks. Moving to another clip
  // cancels the reading, so quick browsing does not queue measurements.
  let target = $derived(clip.status === "ready" ? clip.id : null);
  $effect(() => {
    const id = target;
    if (!id) return;
    const request = new AbortController();
    untrack(() => { checks = { id, items: [] }; });
    void api.captureChecks(id, request.signal).then(result => {
      if (result?.ok && !request.signal.aborted && Array.isArray(result.data?.checks)) checks = { id, items: result.data.checks };
    });
    return () => request.abort();
  });
  let rows = $derived.by(() => {
    const list: Row[] = [
      clip.gyro
        ? { name: "Gyro", value: "Gyro log found", tone: "ok" }
        : { name: "Gyro", value: "No gyro log", tone: "plain" },
      clip.dropped_frames === 0
        ? { name: "Frames", value: "No dropped frames", tone: "ok" }
        : {
            name: "Frames",
            value: clip.filled_frames > 0
              ? `${clip.dropped_frames} dropped, ${clip.filled_frames} filled on export`
              : `${clip.dropped_frames} dropped`,
            tone: "warn",
          },
    ];
    if (clip.recovered_note) list.push({ name: "Recovered", value: clip.recovered_note, tone: "warn" });
    list.push(clip.camera_profile
      ? { name: "Camera profile", value: clip.camera_profile, tone: "ok" }
      : { name: "Camera profile", value: "No camera profile", tone: "plain" });
    if (checks.id === clip.id) list.push(...checks.items);
    return list;
  });
</script>

<dl aria-label="Readiness">
  {#each rows as row (`${row.name}:${row.value}`)}
    <div class="row">
      <dt>{row.name}</dt>
      <dd class={row.tone}>{row.value}</dd>
    </div>
  {/each}
</dl>

<style>
  dl {
    margin: 0;
  }
  .row {
    display: grid;
    grid-template-columns: 110px 1fr;
    gap: 8px;
    padding: 6px 0;
    border-top: 1px solid var(--line);
    font-size: 12px;
  }
  dt {
    color: var(--muted);
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .ok {
    color: var(--accent);
  }
  .warn {
    color: var(--warn);
  }
</style>
