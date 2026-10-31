<script lang="ts">
  import { api, type ApiError, type Schemas } from "../lib/api";
  import ReportActions from "./ReportActions.svelte";
  import ErrorNote from "./ErrorNote.svelte";

  let report = $state<Schemas["SupportReport"] | null>(null);
  let busy = $state(false);
  let problem = $state<ApiError | null>(null);
  async function collect(): Promise<void> {
    busy = true; problem = null; report = null;
    const result = await api.supportReport();
    if (result.ok) report = result.data;
    else problem = result;
    busy = false;
  }
</script>

<section aria-label="Support diagnostics">
  <h2>Help and support</h2>
  <p>Collect system checks, dependency versions and available engine logs. Each file is limited to its last 64 KiB. Home-folder paths are shortened to ~; file and folder names remain. Review the report before sharing it. Nothing is sent automatically.</p>
  <p>Recordings, pictures, sound, gyro samples and clip metadata are excluded. For a report with selected clip metadata or sidecars, use <code>mcrawsome support-bundle</code> in a terminal. A manually started engine may write logs only to its terminal.</p>
  <button class="secondary" disabled={busy} onclick={() => void collect()}>{busy ? "Working…" : report ? "Refresh support report" : "Collect support report"}</button>
  {#if problem}<ErrorNote error={problem} />{/if}
  {#if report}
    <ReportActions value={report} filename="mcrawsome-support.json" label="support report" />
    <details><summary>Review support report</summary><textarea aria-label="Support report" readonly value={JSON.stringify(report, null, 2)} spellcheck="false"></textarea></details>
  {/if}
</section>

<style>
  section { margin: 24px 0; padding: 16px; border: 1px solid var(--line); border-radius: 8px; }
  h2 { margin-top: 0; font-size: 16px; }
  p { font-size: 13px; line-height: 1.5; color: var(--muted); }
  button { margin: 0 8px 8px 0; }
  details { margin-top: 8px; } summary { cursor: pointer; }
  textarea { width: 100%; height: 320px; box-sizing: border-box; margin-top: 12px; font: 12px var(--mono); }
</style>
