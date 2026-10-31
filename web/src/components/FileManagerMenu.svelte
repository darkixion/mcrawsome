<script lang="ts">
  import { onDestroy } from "svelte";
  import { bridge } from "../lib/bridge";
  import type { FileMenuStatus } from "../lib/file-manager-menu";
  import InfoTip from "./InfoTip.svelte";
  let status = $state<FileMenuStatus | null>(null);
  let changing = $state(false);
  let problem = $state("");
  let alive = true;
  let input: HTMLInputElement;
  $effect(() => {
    const api = $bridge?.api;
    if (api?.file_menu_status) void api.file_menu_status().then(value => { if (alive) status = value; }).catch(() => { if (alive) problem = "Could not check file-manager support."; });
  });
  onDestroy(() => { alive = false; });
  async function change(enabled: boolean): Promise<void> {
    const api = $bridge?.api;
    if (!api?.set_file_menu) return;
    changing = true; problem = "";
    try {
      const result = await api.set_file_menu(enabled);
      if (!alive) return;
      status = result.status;
      if (!result.ok && !result.cancelled) problem = result.message ?? "Could not change the file-manager menu.";
    } catch { if (alive) problem = "Could not change the file-manager menu."; }
    finally { changing = false; if (input) input.checked = status?.enabled ?? false; }
  }
</script>
<h3>File manager</h3>
<div class="file-menu-option">
  <label class="check"><input bind:this={input} type="checkbox" role="switch" checked={status?.enabled ?? false} disabled={changing || !status?.supported || (status.requires_admin && !status.can_install_support && !status.enabled)} onchange={(event) => void change(event.currentTarget.checked)} />MCRAWsome file menu</label>
  <InfoTip label="MCRAWsome file menu" text="Enabled by default when the Debian package is installed. Adds an MCRAWsome submenu for selected .mcraw recordings, with Send to Resolve (Media Bin) and Send to Resolve (Timeline), without opening the MCRAWsome window. Currently supports GNOME Files (Nautilus). If Nautilus's Python support is missing, installing it needs administrator access and a system password prompt. Disabling hides the menu for your account and keeps your choice across package upgrades." />
</div>
{#if changing}<p class="menu-state" role="status">Updating file-manager integration…</p>
{:else if status?.requires_admin}<p class="menu-state admin">Administrator access required to install Nautilus support.</p>
{:else if status?.enabled && status.current}<p class="menu-state">Restart Files after installing or updating the package.</p>
{:else if !status?.supported}<p class="menu-state">{status?.message ?? "Manage this menu in the Debian desktop application."}</p>{/if}
{#if status?.enabled && (!status.current || status.requires_admin)}<button class="secondary" disabled={changing} onclick={() => void change(true)}>Update file menu</button>{/if}
{#if problem}<p class="menu-state error" role="alert">{problem}</p>{/if}
<style>
  h3 { margin: 20px 0 8px; font-size: 13px; font-weight: 600; }
  .file-menu-option { display: flex; align-items: center; gap: 5px; min-height: 32px; font-size: 13px; }
  .check { display: flex; align-items: center; gap: 9px; }
  .check input { flex: 0 0 auto; }
  .menu-state { margin: 5px 0 8px; font-size: 12px; color: var(--muted); }
  .admin { color: var(--warn); }
  .error { color: var(--fail); }
</style>
