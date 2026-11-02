<script lang="ts">
  import { formatClock } from "../lib/format";
  import { onMount } from "svelte";
  import { obscure } from "../lib/obscuring";
  import { waveColours } from "../lib/wave-colour";
  // The playhead slider, with the In and Out marks as handles at either end of the marked range. A handle is
  // dragged (or moved with the arrow keys once focused) and the range is kept when it is let go.
  let { index, count, start, end, rate = 0, showRange = true, edge = false, peaks = [], onseek, onrest = () => {}, ontrim = () => {}, onpreview = () => {} }: {
    index: number; count: number; start: number; end: number; rate?: number; showRange?: boolean; edge?: boolean;
    peaks?: number[]; // the clip's sound: the loudest sample in each equal part, as a fraction of full scale
    onseek: (index: number) => void; onrest?: () => void;
    ontrim?: (start: number, end: number) => void; onpreview?: (index: number) => void;
  } = $props();
  // The sound on a decibel scale from -48 dB (flat) to full scale (the whole height), so quiet phone sound
  // still shows; mirrored about the middle.
  const level = (peak: number) => Math.max(0, Math.min(1, (20 * Math.log10(Math.max(peak, 1e-6)) + 48) / 48));
  let wave = $derived.by(() => {
    if (peaks.length === 0) return "";
    const edge = (sign: number) => peaks.map((peak, part) => `${part + 0.5},${(50 - sign * 50 * level(peak)).toFixed(1)}`);
    return `M${edge(1).join("L")}L${edge(-1).reverse().join("L")}Z`;
  });
  const identity = $props.id();
  const LAYERS = ["all", "kept"] as const;
  let colours = $derived(waveColours(peaks));
  let bar: HTMLDivElement;
  let hovered = $state(false), focused = $state(false), touch = $state(false), barWidth = $state(0);
  let dragging = $state<{ which: "in" | "out"; start: number; end: number } | null>(null);
  let shownStart = $derived(dragging?.start ?? start);
  let shownEnd = $derived(dragging?.end ?? end);
  const total = $derived(Math.max(count, 1));
  let expanded = $derived(hovered || focused || touch || dragging !== null);
  let close = $derived(barWidth > 0 && (shownEnd - shownStart) / total * barWidth < 28);
  // Native video draws above HTML. Reserve only the seek rail until its grips are revealed.
  $effect(() => { void expanded; void close; if (edge && bar) return obscure(bar); });
  onMount(() => {
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => { barWidth = bar.clientWidth; });
    observer.observe(bar); return () => observer.disconnect();
  });
  const percent = (frame: number) => `${(100 * frame) / total}%`;
  function frameAt(clientX: number): number {
    const box = bar.getBoundingClientRect();
    return Math.round(Math.max(0, Math.min(1, (clientX - box.left) / Math.max(box.width, 1))) * count);
  }
  function moved(which: "in" | "out", frame: number, from = { start, end }) {
    const next = which === "in"
      ? { which, start: Math.max(0, Math.min(frame, from.end - 1)), end: from.end }
      : { which, start: from.start, end: Math.min(count, Math.max(frame, from.start + 1)) };
    onpreview(which === "in" ? next.start : next.end - 1);
    return next;
  }
  function grab(which: "in" | "out", event: PointerEvent) {
    if (event.button !== 0) return;
    event.preventDefault();
    (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
    dragging = moved(which, frameAt(event.clientX));
  }
  function drag(event: PointerEvent) {
    if (dragging) dragging = moved(dragging.which, frameAt(event.clientX));
  }
  function release() {
    if (!dragging) return;
    const { start: first, end: last } = dragging;
    dragging = null;
    if (first !== start || last !== end) ontrim(first, last);
  }
  function step(which: "in" | "out", event: KeyboardEvent) {
    const direction = event.key === "ArrowLeft" || event.key === "ArrowDown" ? -1 : event.key === "ArrowRight" || event.key === "ArrowUp" ? 1 : 0;
    const home = event.key === "Home", finish = event.key === "End";
    if (!direction && !home && !finish) return;
    event.preventDefault();
    const current = which === "in" ? start : end;
    const target = home ? (which === "in" ? 0 : start + 1) : finish ? (which === "in" ? end - 1 : count) : current + direction * (event.shiftKey ? Math.max(1, Math.round(rate)) : 1);
    const next = moved(which, target);
    if (next.start !== start || next.end !== end) ontrim(next.start, next.end);
  }
  // Minutes, seconds and hundredths: a mark is placed to the frame.
  function stamp(frame: number): string {
    if (rate <= 0) return "";
    const seconds = frame / rate, hundredths = String(Math.floor((seconds % 1) * 100 + 1e-6)).padStart(2, "0");
    return `${formatClock(Math.floor(seconds))}.${hundredths}`;
  }
</script>
<div class="bar" class:marked={showRange} class:edge class:expanded class:close bind:this={bar!}
  style={`--range-start: ${showRange ? percent(shownStart) : "0%"}; --range-end: ${showRange ? percent(shownEnd) : "100%"}`}
  onpointerenter={() => hovered = true} onpointerleave={() => { hovered = false; if (!dragging) touch = false; }}
  onpointerdown={(event) => { if (event.pointerType === "touch") touch = true; }}
  onfocusin={() => focused = true} onfocusout={(event) => focused = event.relatedTarget instanceof Node && bar.contains(event.relatedTarget)} role="group" aria-label="Preview timeline">
  {#if wave}{#each LAYERS as part (part)}<svg class={`wave ${part}`} viewBox={`0 0 ${peaks.length} 100`} preserveAspectRatio="none" aria-hidden="true">
    <defs><linearGradient id={`${identity}-${part}`} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2={peaks.length} y2="0">{#each colours[part] as stop}<stop offset={stop.offset} stop-color={stop.colour} />{/each}</linearGradient></defs>
    <path d={wave} fill={`url(#${identity}-${part})`} />
  </svg>{/each}{/if}
  {#if showRange}<div class="track"><div aria-label="Trim range" style={`left: ${percent(shownStart)}; width: ${percent(shownEnd - shownStart)}`}></div></div>{/if}
  <input type="range" aria-label="Position" min="0" max={Math.max(0, count - 1)} step="1" value={index} oninput={(event) => onseek(Number(event.currentTarget.value))} onchange={onrest} style={`--progress: ${100 * index / Math.max(1, count - 1)}%`} />
  {#if showRange}
    {#each [["in", shownStart, 0, Math.max(0, end - 1)], ["out", shownEnd, start + 1, count]] as [which, frame, low, high] (which)}
      {@const name = which === "in" ? "In" : "Out"}
      <div class={`handle ${which}`} class:active={dragging?.which === which} role="slider" tabindex="0" aria-label={`${name} mark`}
        aria-valuemin={Number(low)} aria-valuemax={Number(high)} aria-valuenow={Number(frame)}
        aria-valuetext={`${name} frame ${which === "in" ? Number(frame) + 1 : Number(frame)}, ${stamp(Number(frame))}`}
        title={`Drag to move the ${name} mark; the mounted media and exports use the marked range`}
        style:left={edge ? `clamp(7px, ${percent(Number(frame))}, calc(100% - 7px))` : percent(Number(frame))}
        onpointerdown={(event) => grab(which as "in" | "out", event)} onpointermove={drag} onpointerup={release} onpointercancel={release} onlostpointercapture={release}
        onkeydown={(event) => step(which as "in" | "out", event)}></div>
    {/each}
    <div class="labels" aria-hidden="true"><span>In {shownStart + 1} · {stamp(shownStart)}</span><span>Out {shownEnd} · {stamp(shownEnd)}</span></div>
  {/if}
</div>
<style>
  .bar { position: relative; margin: 14px 0 8px; }
  .bar.marked { padding-bottom: 12px; }
  .track { height: 4px; background: var(--line); position: relative; }
  .wave { display: block; width: 100%; height: 20px; pointer-events: none; }
  .wave.kept { position: absolute; top: 0; left: 0; clip-path: inset(0 calc(100% - var(--range-end)) 0 var(--range-start)); }
  .track div { position: absolute; height: 4px; background: var(--accent); }
  input { width: 100%; margin: 6px 0; accent-color: var(--accent); }
  .handle { position: absolute; top: -5px; width: 9px; height: 14px; margin-left: -4.5px; cursor: ew-resize; touch-action: none; border: 1px solid var(--accent); background: var(--panel); }
  .handle.in { border-radius: 3px 0 0 3px; border-right-width: 3px; margin-left: -8px; }
  .handle.out { border-radius: 0 3px 3px 0; border-left-width: 3px; margin-left: -1px; }
  .handle:hover, .handle.active, .handle:focus-visible { background: var(--accent); outline: none; }
  .labels { position: absolute; left: 0; right: 0; bottom: 0; display: flex; justify-content: space-between; gap: 8px; pointer-events: none; font-size: 9px; line-height: 11px; color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; opacity: .8; }
  .edge, .edge.marked { position: absolute; z-index: 4; bottom: 0; left: 0; right: 0; height: 14px; margin: 0; padding: 0; background: var(--panel); }
  .edge.expanded { height: 60px; }
  .edge.expanded.close { height: 84px; }
  .edge .track { position: absolute; left: 0; right: 0; bottom: 4px; height: 5px; border: 0; }
  .edge .track div { height: 5px; opacity: .35; }
  .edge input { --played-end: clamp(var(--range-start), var(--progress), var(--range-end)); --played-track: linear-gradient(to right, transparent var(--range-start), var(--accent) var(--range-start) var(--played-end), transparent var(--played-end)); appearance: none; position: absolute; bottom: 0; left: 0; height: 14px; margin: 0; background: transparent; }
  .edge input::-webkit-slider-runnable-track { height: 4px; background: var(--played-track); border-radius: 2px; }
  .edge input::-webkit-slider-thumb { appearance: none; width: 12px; height: 12px; margin-top: -4px; border-radius: 50%; border: 2px solid var(--panel); background: var(--accent); }
  .edge input::-moz-range-track { height: 4px; background: var(--played-track); }
  .edge input::-moz-range-progress { height: 4px; background: transparent; }
  .edge input::-moz-range-thumb { width: 9px; height: 9px; border-radius: 50%; border: 2px solid var(--panel); background: var(--accent); }
  .edge .wave { position: absolute; top: auto; left: 0; bottom: 16px; height: 22px; opacity: 0; transition: opacity .1s; }
  .edge.expanded .wave { opacity: 1; }
  .edge .handle { box-sizing: border-box; top: auto; bottom: 16px; height: 22px; width: 14px; margin-left: -7px; opacity: 0; pointer-events: none; border: 2px solid var(--accent); border-radius: 3px; transition: opacity .1s; }
  .edge .handle.in { box-shadow: inset 4px 0 var(--accent); }
  .edge .handle.out { box-shadow: inset -4px 0 var(--accent); }
  .edge.expanded .handle { opacity: 1; pointer-events: auto; }
  .edge.close .handle.out { bottom: 40px; }
  .edge .labels { bottom: 42px; opacity: 0; font-size: 10px; line-height: 14px; }
  .edge.close .labels { bottom: 66px; }
  .edge.expanded .labels { opacity: 1; }
  .edge .handle:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }
  @media (hover: none) { .edge { height: 40px; } .edge.close { height: 64px; } .edge .handle { opacity: 1; pointer-events: auto; } }
  @media (prefers-reduced-motion: reduce) { .edge .handle, .edge .wave { transition: none; } }
  @media (forced-colors: active) { .edge .handle, .edge .track div { border-color: Highlight; } }
</style>
