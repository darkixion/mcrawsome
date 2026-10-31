<script lang="ts" generics="T extends string | number">
  import { tick } from "svelte";
  import Icon, { type IconName } from "./Icon.svelte";
  import { obscure } from "../lib/obscuring";
  // A compact drop-down for the viewer's top edge: an icon button that opens a list of choices.
  type Item = { value: T; label: string; icon?: IconName; disabled?: boolean; title?: string };
  let { icon, label, title, items, value, shown = "", align = "left", onselect }: {
    icon: IconName; label: string; title: string; items: Item[]; value: T; shown?: string;
    align?: "left" | "right"; onselect: (value: T) => void;
  } = $props();
  const identity = $props.id();
  let open = $state(false);
  let root: HTMLDivElement;
  let list = $state<HTMLDivElement>();
  // Placed against the window, so the viewer's frame (which clips its picture) never cuts the list off.
  // In a viewer the list is a sheet as wide as the viewer: the desktop app's native picture, drawn above the
  // page, can give up only a whole band of itself to a pop-up, and beside a narrower list it would leave
  // the page bare (native-preview.ts `rectangle`).
  let place = $state("");
  let sheet = $state(false);
  function position() {
    const box = root.querySelector("button")!.getBoundingClientRect();
    const top = box.bottom + 4, room = Math.max(120, window.innerHeight - top - 8);
    const viewer = root.closest<HTMLElement>("[data-mcrawsome-viewport]")?.getBoundingClientRect();
    sheet = !!viewer;
    const side = viewer ? `left: ${viewer.left}px; width: ${viewer.width}px`
      : align === "left" ? `left: ${Math.max(4, box.left)}px` : `right: ${Math.max(4, window.innerWidth - box.right)}px`;
    place = `top: ${top}px; ${side}; max-height: ${room}px`;
  }
  async function toggle() {
    open = !open;
    if (!open) return;
    position();
    await tick();
    (list?.querySelector<HTMLElement>('[aria-checked="true"]') ?? list?.querySelector<HTMLElement>("[role=menuitemradio]:not(:disabled)"))?.focus();
  }
  $effect(() => { if (open && list) return obscure(list); });
  function choose(item: Item) {
    if (item.disabled) return;
    open = false;
    onselect(item.value);
    root.querySelector<HTMLElement>("button")?.focus();
  }
  function keys(event: KeyboardEvent) {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); open = false; root.querySelector<HTMLElement>("button")?.focus(); return; }
    const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[event.key];
    if (step === undefined || (!sheet && (event.key === "ArrowLeft" || event.key === "ArrowRight"))) return;
    event.preventDefault();
    const options = [...(list?.querySelectorAll<HTMLElement>("[role=menuitemradio]:not(:disabled)") ?? [])];
    const at = options.indexOf(document.activeElement as HTMLElement);
    options[(at + step + options.length) % options.length]?.focus();
  }
  function outside(event: PointerEvent) { if (open && !root.contains(event.target as Node)) open = false; }
  function moved(event: Event) { if (open && !(event.target instanceof Node && list?.contains(event.target))) open = false; }
</script>
<svelte:window onpointerdown={outside} onresize={() => { open = false; }} onscrollcapture={moved} />
<div class="menu" bind:this={root!} onkeydown={keys} role="presentation">
  <button class="trigger" class:on={shown !== ""} aria-label={label} aria-haspopup="menu" aria-expanded={open} aria-controls={`${identity}-list`} {title} onclick={() => void toggle()}>
    <Icon name={icon} />{#if shown}<span>{shown}</span>{/if}
  </button>
  {#if open}
    <div bind:this={list} id={`${identity}-list`} class="list" class:sheet style={place} role="menu" aria-label={label} aria-orientation={sheet ? "horizontal" : "vertical"}>
      {#each items as item (item.value)}
        <button role="menuitemradio" aria-checked={item.value === value} disabled={item.disabled} title={item.title} onclick={() => choose(item)}>
          {#if item.icon}<Icon name={item.icon} />{/if}<span>{item.label}</span>
        </button>
      {/each}
    </div>
  {/if}
</div>
<style>
  .menu { position: relative; }
  .trigger { display: flex; align-items: center; gap: 4px; height: 26px; padding: 2px 6px; background: transparent; color: var(--text); border: 1px solid transparent; font-size: 11px; }
  .trigger:hover, .trigger[aria-expanded="true"] { background: var(--card); }
  .trigger.on { color: var(--accent); }
  .trigger :global(svg) { width: 17px; height: 17px; }
  .list { position: fixed; z-index: 50; display: grid; min-width: 170px; overflow-y: auto; padding: 4px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: 0 8px 24px #0006; }
  .list button { display: flex; align-items: center; gap: 8px; padding: 5px 8px; background: transparent; color: var(--text); border: 0; text-align: left; font-size: 12px; white-space: nowrap; }
  .list button:hover:not(:disabled), .list button:focus-visible { background: var(--card); outline: none; }
  .list button[aria-checked="true"] { color: var(--accent); }
  .list button:disabled { color: var(--muted); opacity: .6; }
  .list :global(svg) { width: 16px; height: 16px; flex: none; }
  .sheet { display: flex; flex-wrap: wrap; align-content: flex-start; gap: 2px 4px; min-width: 0; box-sizing: border-box; border-radius: 0 0 var(--radius) var(--radius); }
</style>
