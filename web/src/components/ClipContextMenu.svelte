<script lang="ts">
  import Icon from "./Icon.svelte";
  import { onMount } from "svelte";
  import { sendChoices, sendKey, sendActivity, type SendTarget } from "../lib/send";
  import type { SelectionAction } from "../lib/vignette";
  import { obscure } from "../lib/obscuring";
  let { x, y, raw, mounted, count, sourceName, busy = false, inUse = false, canRemove = false, canOpenFolder, mountAvailable = true, onaction, onclose }: {
    canRemove?: boolean;
    mountAvailable?: boolean;
    x: number; y: number; raw: boolean; mounted: boolean; canOpenFolder: boolean;
    count: number; sourceName: string; busy?: boolean; inUse?: boolean;
    onaction: (action: SelectionAction, target?: SendTarget) => void;
    onclose: () => void;
  } = $props();
  let menu: HTMLDivElement;
  let left = $state(0);
  let top = $state(0);
  let returnFocus: HTMLElement | null = null;
  onMount(() => {
    const previous = document.activeElement;
    returnFocus = previous instanceof HTMLElement ? previous : null;
    left = Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8));
    top = Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8));
    menu.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    const outside = (event: PointerEvent) => { if (event.target instanceof Node && !menu.contains(event.target)) onclose(); };
    const close = () => onclose();
    window.addEventListener("pointerdown", outside);
    window.addEventListener("resize", close);
    const release = obscure(menu);
    return () => { release(); window.removeEventListener("pointerdown", outside); window.removeEventListener("resize", close); if (previous instanceof HTMLElement && previous.isConnected && menu.contains(document.activeElement)) previous.focus(); };
  });
  function key(event: KeyboardEvent): void {
    if (event.key === "Escape") { event.preventDefault(); returnFocus?.focus(); onclose(); return; }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const buttons = [...menu.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    buttons[event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length]?.focus();
  }
</script>
<div bind:this={menu} role="menu" aria-label="Clip actions" tabindex="-1" style:left="{left}px" style:top="{top}px" onkeydown={key}>
  <p>{count} selected</p>
  {#if raw}
    <button role="menuitem" disabled={busy || (mounted ? inUse : !mountAvailable)} title={mounted && inUse ? "Media has open files in a receiving application" : undefined} onclick={() => onaction(mounted ? "unmount" : "mount")}><Icon name={mounted ? "unmount" : "mount"} /><span>{mounted ? "Unmount" : "Mount"}</span></button>
    {#each sendChoices as target (sendKey(target))}<button role="menuitem" disabled={$sendActivity?.busy || busy} onclick={() => onaction("send", target)}><Icon name={target.mode === "timeline" ? "frames" : "export"} /><span>Send to {target.label}</span></button>{/each}
    <hr />
    <button role="menuitem" disabled={busy} title={`Source: ${sourceName}`} onclick={() => onaction("profile-export")}><Icon name="vignette" /><span>Export Vignette Profile...</span></button>
    <button role="menuitem" disabled={busy} onclick={() => onaction("profile-apply")}><Icon name="import" /><span>Apply Vignette Profile to Selected...</span></button>
    <button role="menuitem" disabled={busy} onclick={() => onaction("profile-clear")}><Icon name="reset" /><span>Clear Vignette Profile Override</span></button>
    <hr />
  {/if}
  <button role="menuitem" disabled={!canOpenFolder} title={canOpenFolder ? "Open the source folder" : "Available in the desktop app"} onclick={() => onaction("folder")}><Icon name="folder" /><span>Open Folder</span></button>
  {#if mounted}<hr /><button role="menuitem" disabled={!canOpenFolder} title={canOpenFolder ? "Open the selected virtual media folders" : "Available in the desktop app"} onclick={() => onaction("mount-folder")}><Icon name="folder" /><span>Open Mount Folder</span></button>{/if}
  <hr /><button role="menuitem" disabled={busy || inUse} title={inUse ? "Media has open files in a receiving application" : "Unmount, then move the recordings and their companion files to the Trash"} onclick={() => onaction("trash")}><Icon name="trash" /><span>Unmount and Move to Trash...</span></button>
  {#if canRemove}<hr /><button role="menuitem" disabled={busy} title="Remove references from this session. Original files and mounted media are kept." onclick={() => onaction("remove")}><Icon name="off" /><span>Remove from session</span></button>{/if}
</div>
<style>
  [role="menu"] { position: fixed; z-index: 100; display: flex; flex-direction: column; min-width: 235px; max-height: calc(100vh - 16px); overflow-y: auto; padding: 6px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: 0 12px 36px rgb(0 0 0 / 40%); }
  button { display: flex; align-items: center; gap: 10px; text-align: left; background: transparent; color: var(--text); border-radius: 4px; padding: 9px 12px; }
  button :global(svg) { width: 17px; height: 17px; flex-shrink: 0; color: var(--muted); }
  button:hover, button:focus-visible { background: var(--card); outline: 1px solid var(--accent); }
  button:disabled { color: var(--muted); opacity: 0.6; }
  hr { width: 100%; border: 0; border-top: 1px solid var(--line); }
  p { margin: 6px 12px; color: var(--muted); font-size: 11px; }
</style>
