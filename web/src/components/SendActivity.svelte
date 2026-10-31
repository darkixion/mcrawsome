<script lang="ts">
  import { sendActivity } from "../lib/send";
  let entries = $derived($sendActivity?.entries ?? []);
  let successes = $derived(entries.filter(entry => entry.state === "sent").length);
  let failures = $derived(entries.filter(entry => entry.state === "failed").length);
  let mounting = $derived($sendActivity?.busy && $sendActivity.phase === "mounting");
  let prepared = $derived(entries.filter(entry => entry.preparation === "ready" || entry.preparation === "failed").length);
  let summary = $derived(!$sendActivity ? "" : mounting ? `Preparing media for ${$sendActivity.targetLabel}: ${prepared} of ${entries.length} complete` : $sendActivity.busy ? `Sending ${entries.length} ${entries.length === 1 ? "clip" : "clips"} to ${$sendActivity.targetLabel}${$sendActivity.stage ? ` · ${$sendActivity.stage}` : "…"}`
    : failures === entries.length ? `Could not send to ${$sendActivity.targetLabel}`
    : `${successes} ${successes === 1 ? "clip" : "clips"} sent to ${$sendActivity.targetLabel}${failures ? ` · ${failures} failed` : ""}`);
</script>
{#if $sendActivity}
  <div class="activity" role="status" aria-label="Send activity" aria-live="polite" aria-busy={$sendActivity.busy}>
    {#if mounting}
      <progress aria-label="Mount preparation" value={prepared} max={entries.length}></progress>
    {:else if $sendActivity.busy}
      <progress aria-label="Creating and sending handoff"></progress>
    {/if}
    <details open={failures > 0 && !$sendActivity.busy}>
      <summary class:failed={failures > 0}>{summary}</summary>
      <div class="outcomes">
        {#if successes}<p>The handoff was dispatched. Complete the import in the receiving application. Keep source recordings available while virtual media is in use.</p>{/if}
        {#each entries as entry (entry.id)}
          <p><strong>{entry.name}</strong> · {entry.message || "Sending…"}</p>
          {#if entry.archive}<p class="path">{entry.archive}</p>{/if}
        {/each}
      </div>
    </details>
  </div>
{/if}
<style>
  .activity { margin: -6px 0 14px; font-size: 12px; color: var(--accent); }
  progress { display: block; width: min(280px, 100%); height: 4px; margin-bottom: 6px; accent-color: var(--accent); }
  summary { cursor: pointer; width: fit-content; }
  .failed { color: var(--fail); }
  .outcomes { margin-top: 8px; color: var(--muted); }
  p { margin: 4px 0; overflow-wrap: anywhere; }
  .path { font: 11px var(--mono); user-select: text; }
</style>
