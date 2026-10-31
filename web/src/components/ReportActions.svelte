<script lang="ts">
  import { tick } from "svelte";
  import { get } from "svelte/store";
  import { bridge } from "../lib/bridge";
  let { value, filename, label }: { value: unknown; filename: string; label: string } = $props();
  let message = $state("");
  let fallback = $state("");
  let text = $state<HTMLTextAreaElement>();
  let busy = $state(false);
  const format = $derived(filename.endsWith(".meta") ? "metadata" : "JSON");
  const json = () => JSON.stringify(value, null, 2) + "\n";
  async function copy(): Promise<void> {
    const content = json();
    try { await navigator.clipboard.writeText(content); message = `${format} copied`; fallback = ""; }
    catch { fallback = content; message = `Select and copy the ${format} below`; await tick(); text?.focus(); text?.select(); }
  }
  async function save(): Promise<void> {
    if (busy) return;
    busy = true;
    try {
      const content = json();
      if (new Blob([content]).size > 8 * 1024 * 1024) throw new Error("JSON report exceeds 8 MiB");
      const native = get(bridge)?.api.save_report;
      if (native) { message = await native(content, filename) ? `${format} saved` : "Save cancelled"; }
      else {
        const url = URL.createObjectURL(new Blob([content], { type: "application/json" }));
        const link = document.createElement("a"); link.href = url; link.download = filename; link.click();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000); message = `${format} downloaded`;
      }
    } catch (error) { message = error instanceof Error ? error.message : `Could not save ${format}`; }
    finally { busy = false; }
  }
</script>
<div class="report-actions">
  <button class="secondary" aria-label={`Copy ${label} ${format}`} onclick={() => void copy()}>Copy {format}</button>
  <button class="secondary" aria-label={`Save ${label} ${format}`} disabled={busy} onclick={() => void save()}>Save {format}</button>
  {#if message}<span role="status">{message}</span>{/if}
</div>
{#if fallback}<label class="manual">{label} JSON<textarea bind:this={text} readonly value={fallback} aria-label={`${label} ${format}`}></textarea></label>{/if}
<style>
  .report-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 12px 0; }
  button { font-size: 11px; padding: 6px 9px; background: transparent; border: 1px solid var(--line); color: var(--text); }
  span, .manual { color: var(--muted); font-size: 11px; } .manual { display: grid; gap: 6px; }
  textarea { width: 100%; min-height: 100px; font-family: var(--mono); font-size: 11px; }
</style>
