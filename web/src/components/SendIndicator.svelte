<script lang="ts">
  import { clipSends, sendActivity, sendHistory } from "../lib/send";
  let { id }: { id: string } = $props();
  let entries = $derived(clipSends(id, $sendHistory, $sendActivity));
  let pending = $derived(entries.filter(entry => entry.state === "sending"));
  let sent = $derived(entries.filter(entry => entry.state === "sent"));
  let failed = $derived(entries.filter(entry => entry.state === "failed"));
</script>
{#if entries.length}
  <span class="send-state" aria-label="Send status" title={entries.map(entry => `${entry.targetLabel}: ${entry.message || "Sending RAW video and WAV audio"}${entry.archive ? `\n${entry.archive}` : ""}`).join("\n\n")}>
    {#if pending.length}<span class="pending">Sending to {pending.map(entry => entry.targetLabel).join(", ")}…</span>{/if}
    {#if sent.length}<span>✓ Sent to {sent.map(entry => entry.targetLabel).join(", ")}</span>{/if}
    {#if failed.length}<span class="failed">Send to {failed.map(entry => entry.targetLabel).join(", ")} failed</span>{/if}
  </span>
{/if}
<style>
  .send-state { display: inline-flex; gap: 6px; flex-wrap: wrap; color: var(--accent); font-size: 11px; margin-top: 6px; }
  .failed { color: var(--fail); }
  .pending { color: var(--muted); }
</style>
