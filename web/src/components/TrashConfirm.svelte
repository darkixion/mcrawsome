<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import type { ClipSummary } from "../lib/library";
  let { chosen, onclose, onchanged }: {
    chosen: ClipSummary[]; onclose: () => void; onchanged: () => void;
  } = $props();
  let busy = $state(false);
  let message = $state("");
  let failed = $state(false);
  let keptParts = $state<string[]>([]);
  let panel: HTMLElement;
  // Every library copy of a recording moves, as the engine moves them.
  let files = $derived([...new Set(chosen.flatMap(clip => [clip.path, ...clip.paths]))]);
  async function confirm(): Promise<void> {
    busy = true; message = ""; failed = false;
    try {
      const result = await api.trashClips(chosen.map(clip => clip.id!));
      if (!result.ok) { message = result.message; failed = true; return; }
      const { trashed, unmounted, kept_parts: kept = [] } = result.data;
      message = `Moved ${trashed.length} ${trashed.length === 1 ? "file" : "files"} to the Trash${unmounted.length ? ` after unmounting ${unmounted.length}` : ""}. Restore them from the file manager's Trash.`;
      keptParts = kept;
      onchanged();
    } finally { busy = false; }
  }
  onMount(() => panel.querySelector<HTMLButtonElement>("button.danger")?.focus());
</script>
<section bind:this={panel} aria-label="Move to Trash" class="trash">
  <header><strong>Move to Trash</strong><button class="quiet" disabled={busy} onclick={onclose}>{message && !failed ? "Close" : "Cancel"}</button></header>
  {#if !message || failed}
    <p>Unmounts their virtual media, then moves {files.length === 1 ? "this recording" : `these ${files.length} recordings`} with {files.length === 1 ? "its" : "their"} gyro logs, capture metadata and saved settings to the Trash:</p>
    <ul>{#each files as file}<li>{file}</li>{/each}</ul>
    <p class="muted">Resolve projects using the mounted media will show it offline.</p>
    <button class="danger" disabled={busy} onclick={() => void confirm()}>{busy ? "Moving..." : "Move to Trash"}</button>
  {/if}
  {#if message}<p role={failed ? "alert" : "status"}>{message}</p>{/if}
  {#if keptParts.length}<p class="muted">{keptParts.length === 1 ? "Another part of the split recording stays" : "Other parts of the split recordings stay"} where {keptParts.length === 1 ? "it is" : "they are"}:</p><ul>{#each keptParts as part}<li>{part}</li>{/each}</ul>{/if}
</section>
<style>
  .trash { margin: 12px 0; padding: 12px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); font-size: 12px; }
  header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  p, li { overflow-wrap: anywhere; } ul { margin: 6px 0; padding-left: 18px; max-height: 160px; overflow-y: auto; }
  .muted { color: var(--muted); }
  .danger { background: var(--danger, #ef9a91); color: var(--bg, #0f1720); }
  [role="alert"] { color: var(--error, #e8a2a2); }
</style>
