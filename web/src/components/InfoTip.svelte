<script lang="ts">
  import { onDestroy } from "svelte";
  import Icon from "./Icon.svelte";
  let { label, text, warning = false }: { label: string; text: string; warning?: boolean } = $props();
  const id = $props.id();
  let hovered = $state(false);
  let focused = $state(false);
  let dismissed = $state(false);
  let left = $state(12);
  let top = $state(0);
  let above = $state(false);
  let shown = $derived((hovered || focused) && !dismissed);

  // The tip lives at the end of the page, so an ancestor that moves or contains its content (a clip card on
  // hover, or one skipped while out of view) neither shifts nor clips it. A short delay lets the pointer
  // cross from the button to the tip.
  let leaving: ReturnType<typeof setTimeout> | null = null;
  function enter(): void {
    if (leaving !== null) { clearTimeout(leaving); leaving = null; }
    hovered = true;
  }
  function leave(): void {
    if (leaving !== null) clearTimeout(leaving);
    leaving = setTimeout(() => { leaving = null; hovered = false; }, 120);
  }
  function atPageEnd(node: HTMLElement): { destroy: () => void } {
    document.body.append(node);
    return { destroy: () => node.remove() };
  }
  onDestroy(() => { if (leaving !== null) clearTimeout(leaving); });

  function position(element: HTMLElement): void {
    const box = element.getBoundingClientRect();
    left = Math.max(12, Math.min(box.left - 130, window.innerWidth - 312));
    above = window.innerHeight - box.bottom < 170;
    top = above ? box.top - 8 : box.bottom + 8;
    dismissed = false;
  }
</script>
<span class="info" role="presentation" onmouseenter={enter} onmouseleave={leave}>
  <button type="button" class="info-button" class:warning aria-label={warning ? label : `About ${label}`} aria-describedby={shown ? id : undefined}
    onmouseenter={(event) => position(event.currentTarget)}
    onfocus={(event) => { focused = true; position(event.currentTarget); }} onblur={() => focused = false}
    onclick={(event) => { event.stopPropagation(); position(event.currentTarget); }}
    onkeydown={(event) => { event.stopPropagation(); if (event.key === "Escape") { dismissed = true; event.stopPropagation(); } }}>
    <Icon name={warning ? "warning" : "about"} />
  </button>
  {#if shown}<span use:atPageEnd {id} role="tooltip" class:above style:left={`${left}px`} style:top={`${top}px`} onmouseenter={enter} onmouseleave={leave}>{text}</span>{/if}
</span>
<style>
  .info { display: inline-flex; flex: 0 0 auto; align-items: center; }
  .info-button { border: 0; background: transparent; color: var(--muted); padding: 3px; display: inline-flex; border-radius: 50%; cursor: help; }
  .info-button:hover, .info-button:focus-visible { color: var(--accent); background: var(--panel); }
  .info-button.warning { color: var(--warn); }
  .info-button :global(svg) { width: 16px; height: 16px; }
  [role="tooltip"] { position: fixed; z-index: 100; width: 300px; max-width: calc(100vw - 24px); padding: 12px 14px; border: 1px solid var(--accent); border-radius: 8px; background: var(--panel); box-shadow: 0 8px 28px #0008; color: var(--text); font-size: 12px; line-height: 1.55; font-weight: 400; white-space: normal; }
  [role="tooltip"]::before { content: ""; position: absolute; left: 0; right: 0; top: -10px; height: 10px; }
  [role="tooltip"].above { transform: translateY(-100%); }
  [role="tooltip"].above::before { top: auto; bottom: -10px; }
</style>
