<script lang="ts">
  import Icon from "./Icon.svelte";
  import { tick } from "svelte";
  import { obscure } from "../lib/obscuring";
  import { sendActivity, sendChoices, sendKey, preferredTarget, type SendTarget } from "../lib/send";
  let { onclick, count = 1, showCount = false, compact = false, targets = sendChoices }: {
    onclick: (target: SendTarget) => void; count?: number; showCount?: boolean; compact?: boolean; targets?: readonly SendTarget[];
  } = $props();
  let open = $state(false);
  let root: HTMLDivElement;
  let button = $state<HTMLButtonElement>();
  let menu: HTMLDivElement | undefined;
  // A remembered gyro choice (now part of every timeline send) picks the timeline.
  let preferred = $derived($preferredTarget.replace(/:(gyro|rows|unchecked)/g, ""));
  let target = $derived(targets.find(target => sendKey(target) === preferred) ?? targets.find(target => target.id === preferred) ?? targets[0]);
  let label = $derived(showCount ? `Send ${count} RAW ${count === 1 ? "clip" : "clips"} to ${target?.label}` : `Send to ${target?.label}`);
  function hint(destination: SendTarget): string {
    return destination.mode === "timeline"
      ? "Send RAW video and its WAV audio as a timeline. Clips whose Stabilise switch is on in Resolve Settings carry MCRAWsome's Camera Gyro corrections from their settings; pressing Stabilize in Resolve may replace them."
      : "Send RAW video and its WAV audio. Keep the source available while its virtual media is in use.";
  }
  function choose(target: SendTarget): void { open = false; preferredTarget.set(sendKey(target)); onclick(target); }
  $effect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (event.target instanceof Node && !root?.contains(event.target) && !menu?.contains(event.target)) open = false; };
    window.addEventListener("pointerdown", outside);
    const moved = () => { open = false; };
    window.addEventListener("resize", moved);
    window.addEventListener("scroll", moved, true);
    return () => { window.removeEventListener("pointerdown", outside); window.removeEventListener("resize", moved); window.removeEventListener("scroll", moved, true); };
  });
  function close(): void { if (open) { open = false; button?.focus(); } }
  function keyboard(event: KeyboardEvent): void {
    if (event.key === "Escape") { event.preventDefault(); close(); }
    if (open && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const items = [...(menu?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])];
      const at = items.indexOf(document.activeElement as HTMLButtonElement);
      items[event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (at + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
    }
  }
  function floating(node: HTMLDivElement): { destroy: () => void } {
    menu = node;
    const release = obscure(node);
    document.body.appendChild(node);
    const anchor = root.getBoundingClientRect();
    node.style.left = `${Math.max(8, Math.min(anchor.right - node.offsetWidth, window.innerWidth - node.offsetWidth - 8))}px`;
    node.style.top = `${Math.max(8, Math.min(anchor.bottom + 4, window.innerHeight - node.offsetHeight - 8))}px`;
    return { destroy: () => { release(); menu = undefined; node.remove(); } };
  }
</script>
<div bind:this={root} class:compact class="send-action" role="group" aria-label="Send RAW media">
  {#if target}<button bind:this={button} class="secondary" disabled={$sendActivity?.busy || count === 0} onkeydown={(event) => { event.stopPropagation(); keyboard(event); }} title={hint(target)} onclick={(event) => { event.stopPropagation(); choose(target!); }}>{label}</button>{/if}
  {#if targets.length > 1}
    <button class="secondary chooser" onkeydown={(event) => { event.stopPropagation(); keyboard(event); if (!open && event.key === "ArrowDown") { event.preventDefault(); open = true; void tick().then(() => menu?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus()); } }} aria-label="Choose send option" aria-haspopup="menu" aria-expanded={open} disabled={$sendActivity?.busy} onclick={(event) => { event.stopPropagation(); open = !open; }}>▾</button>
    {#if open}<div use:floating role="menu" tabindex="-1" aria-label="Send options" onkeydown={(event) => { event.stopPropagation(); keyboard(event); }}>{#each targets as destination (sendKey(destination))}<button role="menuitem" title={hint(destination)} onclick={(event) => { event.stopPropagation(); choose(destination); }}><Icon name={destination.mode === "timeline" ? "frames" : "export"} /><span>{destination.label}</span></button>{/each}</div>{/if}
  {/if}
</div>
<style>
  .send-action { display: inline-flex; position: relative; vertical-align: middle; }
  button { background: transparent; color: var(--text); border: 1px solid var(--line); font-size: inherit; }
  .compact { font-size: 11px; }
  .compact button { min-height: 28px; padding: 4px 8px; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  [role="menu"] { position: fixed; z-index: 120; max-height: calc(100dvh - 16px); overflow-y: auto; min-width: 150px; padding: 4px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); box-shadow: 0 6px 20px rgb(0 0 0 / 25%); }
  [role="menu"] button { display: flex; align-items: center; gap: 9px; width: 100%; text-align: left; border: 0; }
  [role="menu"] :global(svg) { width: 16px; height: 16px; color: var(--muted); flex-shrink: 0; }
  .chooser { padding-inline: 7px; }
</style>
