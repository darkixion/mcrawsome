<script lang="ts">
  // Lists of the selected clips for productions and other software: a camera report (CSV or Avid ALE)
  // and their recorded positions (GPX). The engine builds them; nothing is sent anywhere.
  import { api } from "../lib/api";
  import { saveText } from "../lib/save-text";
  let { ids, positions }: { ids: string[]; positions: number } = $props();
  let busy = $state(false), message = $state("");
  const KINDS = { csv: "text/csv", ale: "text/plain", gpx: "application/gpx+xml" } as const;
  async function save(format: keyof typeof KINDS): Promise<void> {
    if (busy || ids.length === 0) return;
    busy = true; message = "";
    try {
      const result = await api.clipReport(ids, format);
      if (!result.ok) { message = result.message; return; }
      const outcome = await saveText(result.data.content, result.data.name, KINDS[format]);
      message = outcome === "cancelled" ? "Save cancelled" : `${result.data.name} ${outcome}`;
    } catch { message = "The list could not be saved"; }
    finally { busy = false; }
  }
</script>
<details class="lists">
  <summary>Lists</summary>
  <div class="menu" role="group" aria-label="Lists of selected clips">
    <button class="quiet" disabled={busy || ids.length === 0} onclick={() => void save("csv")} title="Name, date, timecode, lens, exposure, white balance, movement, scene, shot, take, rating, location and notes of each selected clip">Camera report (CSV)</button>
    <button class="quiet" disabled={busy || ids.length === 0} onclick={() => void save("ale")} title="The camera report as an Avid Log Exchange file, for editors that import ALE metadata">Camera report (ALE)</button>
    <button class="quiet" disabled={busy || positions === 0} onclick={() => void save("gpx")} title="A GPX waypoint for each selected clip with a recorded GPS position, for maps and photo tools">Locations (GPX)</button>
  </div>
</details>
{#if message}<span role="status" class="muted">{message}</span>{/if}
<style>
  .lists { position: relative; }
  summary { cursor: pointer; font-size: 12px; padding: 4px 8px; border-radius: 6px; list-style: none; }
  summary::-webkit-details-marker { display: none; }
  summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .menu { position: absolute; z-index: 6; top: calc(100% + 4px); left: 0; display: grid; gap: 2px; padding: 6px; min-width: 190px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); box-shadow: 0 8px 24px #0006; }
  .menu button { text-align: left; font-size: 12px; }
  .muted { font-size: 11px; color: var(--muted); }
</style>
