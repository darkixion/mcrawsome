<script lang="ts">
  import { onDestroy } from "svelte";
  import { bridge } from "../lib/bridge";
  let associated = $state<boolean | null>(null);
  let changing = $state(false);
  let problem = $state("");
  let alive = true;
  $effect(() => {
    const api = $bridge?.api;
    if (api?.file_association) void api.file_association().then(value => { if (alive) associated = value; }).catch(() => { if (alive) problem = "Could not check file association."; });
  });
  onDestroy(() => { alive = false; });
  async function associate(): Promise<void> {
    changing = true; problem = "";
    try {
      const result = await $bridge?.api.associate_files?.() ?? false;
      if (alive) { associated = result; if (!result) problem = "Could not change file association."; }
    } catch { if (alive) problem = "Could not change file association."; }
    finally { changing = false; }
  }
</script>
<h3>File association</h3>
<p>{associated ? "MCRAWsome is the default application for .mcraw recordings." : "Choose MCRAWsome to open .mcraw recordings. You can change this again in Files > Open With."}</p>
<button class="secondary" disabled={changing || !$bridge?.api.associate_files || associated === true} onclick={() => void associate()}>Use MCRAWsome to open .mcraw files</button>
{#if changing}<p role="status">Updating file association…</p>{/if}
{#if problem}<p class="error" role="alert">{problem}</p>{/if}
<style>
  h3 { margin: 20px 0 8px; font-size: 13px; font-weight: 600; }
  p { margin: 5px 0 8px; font-size: 12px; color: var(--muted); }
  .error { color: var(--fail); }
</style>
