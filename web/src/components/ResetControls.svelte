<script lang="ts">
  import { api, type Schemas } from "../lib/api";
  import { settings } from "../lib/settings";
  let { onpreferences }: { onpreferences?: () => Promise<void> } = $props();
  let options = $state<Schemas["ResetView"][] | null>(null);
  let last = $state<Schemas["ResetResult"] | null>(null);
  let busy = $state(false);
  let message = $state("");
  async function load() {
    busy = true;
    const result = await api.resetOptions();
    if (result.ok) {
      options = result.data;
      const backups = await api.resetBackups();
      if (backups.ok) last = backups.data[0] ?? null;
    } else message = result.message;
    busy = false;
  }
  async function refreshPreferences() {
    if (onpreferences) { await onpreferences(); return; }
    const result = await api.settings();
    if (result.ok) settings.set(result.data);
  }
  async function reset(option: Schemas["ResetView"]) {
    busy = true; message = "";
    const result = await api.resetScope(option.scope, option.version);
    if (result.ok) {
      last = result.data;
      options = options?.map(row => row.scope === option.scope ? { ...row, version: result.data.version } : row) ?? null;
      message = `${option.scope} reset. A recoverable backup was saved.`;
      if (option.scope === "preferences") await refreshPreferences();
    } else message = result.message;
    busy = false;
  }
  async function restore() {
    if (!last) return;
    busy = true; message = "";
    const result = await api.restoreReset(last.backup, last.version);
    if (result.ok) {
      const scope = last.scope;
      last = null; message = "Previous settings restored.";
      if (scope === "preferences") await refreshPreferences();
      await load();
    } else message = result.message;
    busy = false;
  }
</script>
<section aria-label="Reset settings">
  <h2>Reset settings</h2>
  <p>Each operation resets only its stated scope and saves the previous file in a recovery backup. Recordings, sidecars, profiles, named session files and mounted media are preserved.</p>
  <button class="secondary" disabled={busy} onclick={() => void load()}>{options ? "Refresh reset options" : "Show reset options"}</button>
  {#if options}{#each options as option}
    <div class="reset-row"><p>{option.description}</p><button class="secondary" disabled={busy} onclick={() => void reset(option)}>Reset {option.scope}</button></div>
  {/each}{/if}
  {#if message}<p role="status">{message}</p>{/if}
  {#if last}<p>Backup: <code>{last.backup_path}</code></p><button class="secondary" disabled={busy} onclick={() => void restore()}>Restore previous {last.scope}</button>{/if}
</section>
<style>
  section { padding: 20px; border: 1px solid var(--line); border-radius: 8px; margin-top: 20px; }
  h2 { font-size: 14px; } p, code { color: var(--muted); font-size: 12px; line-height: 1.5; }
  code { overflow-wrap: anywhere; } .reset-row { display: flex; justify-content: space-between; gap: 16px; border-top: 1px solid var(--line); padding: 12px 0; }
  button { flex-shrink: 0; align-self: center; }
</style>
