<script lang="ts">
  import { get } from "svelte/store";
  import { bridge } from "../lib/bridge";
  let { id, name, disabled = false }: { id: string; name: string; disabled?: boolean } = $props();
  let busy = $state(false), message = $state("");
  async function save(): Promise<void> {
    busy = true; message = "";
    try {
      const native = get(bridge)?.api.save_capture_csv;
      if (native) message = await native(id, name) ? "Per-frame CSV saved" : "Save cancelled";
      else {
        const response = await fetch(`/api/v1/clips/${encodeURIComponent(id)}/capture.csv`);
        if (!response.ok) throw new Error("Could not read metadata CSV; wait for other analytics to finish and retry");
        const blob = await response.blob();
        const url = URL.createObjectURL(blob), link = document.createElement("a");
        link.href = url; link.download = `${name}.capture.csv`; link.click();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000); message = "Per-frame CSV downloaded";
      }
    } catch (error) { message = error instanceof Error ? error.message : "Could not save metadata CSV"; }
    finally { busy = false; }
  }
</script>
<button disabled={busy || disabled} onclick={() => void save()}>{busy ? "Writing per-frame CSV…" : "Save every frame as CSV"}</button>
{#if message}<p role="status">{message}</p>{/if}
<style>button { font-size: 11px; } p { font-size: 11px; color: var(--muted); }</style>
