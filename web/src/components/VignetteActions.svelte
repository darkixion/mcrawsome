<script lang="ts">
  import { onMount } from "svelte";
  import { get } from "svelte/store";
  import { api } from "../lib/api";
  import { bridge } from "../lib/bridge";
  import { applyProfile, parseProfile, PROFILE_LIMIT, type Profile, type ProfileAction } from "../lib/vignette";
  import type { Applied } from "../lib/bulk-adjustments";
  import type { ClipSummary } from "../lib/library";
  let { action, chosen, source, onclose, onchanged }: {
    action: ProfileAction; chosen: ClipSummary[]; source: ClipSummary | null;
    onclose: () => void; onchanged: () => void;
  } = $props();
  let busy = $state(true);
  let message = $state("");
  let results = $state<Applied[]>([]);
  let upload = $state(false);
  const label = (id: string) => chosen.find(clip => clip.id === id)?.name ?? id;
  async function apply(profile: Profile | null): Promise<void> {
    busy = true; upload = false; message = "";
    try {
      await applyProfile(chosen.map(clip => clip.id!), profile, result => { results = [...results, result]; });
      if (results.some(result => result.ok)) onchanged();
    } finally { busy = false; }
  }
  async function load(file: File | undefined): Promise<void> {
    if (!file) return;
    try {
      if (file.size > PROFILE_LIMIT) throw new Error("Vignette profile exceeds 2 MiB");
      await apply(parseProfile(JSON.parse(await file.text())));
    } catch (error) { message = error instanceof Error ? error.message : "Could not read this profile"; busy = false; }
  }
  async function start(): Promise<void> {
    try {
      const native = get(bridge)?.api;
      if (action === "profile-clear") { await apply(null); return; }
      if (action === "profile-apply") {
        if (!native?.load_vignette_profile) { upload = true; busy = false; return; }
        const profile = await native.load_vignette_profile();
        if (profile === null) { onclose(); return; }
        await apply(parseProfile(profile)); return;
      }
      if (!source?.id) return;
      const result = await api.vignetteProfile(source.id);
      if (!result.ok) { message = result.message; return; }
      if (native?.save_vignette_profile) {
        if (!(await native.save_vignette_profile(result.data, source.name))) { onclose(); return; }
        message = `Saved profile from ${source.name}`;
      } else {
        const url = URL.createObjectURL(new Blob([JSON.stringify(result.data, null, 2) + "\n"], { type: "application/json" }));
        const link = document.createElement("a"); link.href = url; link.download = `${source.name}.vignette.json`;
        link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        message = `Downloaded profile from ${source.name}`;
      }
    } catch (error) { message = error instanceof Error ? error.message : "The profile file dialog failed"; }
    finally { busy = false; }
  }
  onMount(() => { void start(); });
</script>
<section aria-label="Vignette profile" class="profile">
  <header><strong>Vignette profile</strong><button class="quiet" disabled={busy} onclick={onclose}>Close</button></header>
  {#if action === "profile-export" && source}<p>Source: {source.name}</p>{:else}<p>{chosen.length} selected RAW {chosen.length === 1 ? "clip" : "clips"}. Recording files are kept intact.</p>{/if}
  {#if upload}<label>Choose a profile JSON file (up to 2 MiB)<input type="file" accept=".json,application/json" disabled={busy} onchange={(event) => void load(event.currentTarget.files?.[0])} /></label>{/if}
  {#if busy}<p role="status">{results.length ? `${results.length} / ${chosen.length} processed` : "Reading profile..."}</p>{/if}
  {#if message}<p role="status">{message}</p>{/if}
  {#each results as result (result.id)}<p role={result.ok ? "status" : "alert"}>{label(result.id)}: {result.ok ? action === "profile-clear" ? "Override cleared" : "Applied" : result.message}</p>{/each}
</section>
<style>
  .profile { margin: 12px 0; padding: 12px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); font-size: 12px; }
  header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  p { overflow-wrap: anywhere; } label { display: grid; gap: 8px; } [role="alert"] { color: var(--error, #e8a2a2); }
</style>
