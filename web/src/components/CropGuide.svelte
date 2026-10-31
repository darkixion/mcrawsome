<script lang="ts">
  import { obscure } from "../lib/obscuring";
  // The frame a stabilised Resolve timeline keeps: the gyro crop is a zoom about the centre, so the kept
  // area is the picture scaled by 1 / crop whatever the orientation. The preview itself is unstabilised;
  // once the clip is planned as a send plans it, `corners` moves the kept frame as the stabilisation will
  // (frame `index`'s top left, top right, bottom right and bottom left, as fractions of the picture).
  // In the desktop app the native picture covers this outline and draws its own (viewport.py), but the
  // pointer still reaches the page, so this one keeps the hover tip, which the native picture leaves clear.
  let { width, height, crop, corners = null, index = 0, planning = false, refining = false, reason = null }: {
    width: number; height: number; crop: number; corners?: number[][] | null; index?: number; planning?: boolean; refining?: boolean; reason?: string | null;
  } = $props();
  let w = $derived(width / crop);
  let h = $derived(height / crop);
  let x = $derived((width - w) / 2);
  let y = $derived((height - h) / 2);
  let moving = $derived(corners?.[index] ?? null);
  let kept = $derived(moving
    ? `M${moving[0]! * width} ${moving[1]! * height}L${moving[2]! * width} ${moving[3]! * height}L${moving[4]! * width} ${moving[5]! * height}L${moving[6]! * width} ${moving[7]! * height}Z`
    : `M${x} ${y}H${x + w}V${y + h}H${x}Z`);
  let tip = $state<{ left: number; top: number } | null>(null);
  let tooltip: HTMLElement | undefined = $state();
  $effect(() => { if (tooltip) return obscure(tooltip); });

  let svg: SVGSVGElement | undefined = $state();
  let above = $state(false);
  // Beside the picture, never over it: the native picture would give up the tip's area.
  function follow(event: PointerEvent): void {
    const box = svg!.getBoundingClientRect();
    above = window.innerHeight - box.bottom < 120;
    tip = { left: Math.max(12, Math.min(event.clientX - 130, window.innerWidth - 272)), top: above ? box.top - 8 : box.bottom + 8 };
  }
</script>
<svg bind:this={svg} class="crop-guide" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label={`Stabilise crop ${crop.toFixed(2)}: the outlined frame remains${moving ? ", moving as the stabilisation will" : ""}`}>
  <g role="presentation" onpointermove={follow} onpointerleave={() => tip = null}>
    <path fill-rule="evenodd" d={`M0 0H${width}V${height}H0Z ${kept}`} />
    <path class="line" d={kept} vector-effect="non-scaling-stroke" />
  </g>
</svg>
{#if tip}<span bind:this={tooltip} role="tooltip" class:above style:left={`${tip.left}px`} style:top={`${tip.top}px`}>
  Stabilisation crop ({crop.toFixed(2)}×). The dimmed edges are cropped away so the stabilised picture can move
  without showing its borders; the outlined frame is what remains in Resolve.
  {#if moving}It moves with each frame as Resolve's stabilisation will{#if refining}; refining it with image detail…{:else}.{/if}{:else if planning}Working out how it moves with each frame…{:else if reason}It cannot show the movement: {reason}{/if}
</span>{/if}
<style>
  .crop-guide { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
  path { fill: #000; opacity: .45; pointer-events: visiblePainted; }
  .line { fill: none; stroke: #fff; stroke-opacity: .8; stroke-width: 1.5; stroke-dasharray: 6 4; pointer-events: stroke; }
  [role="tooltip"] { position: fixed; z-index: 100; width: 260px; padding: 10px 12px; border: 1px solid var(--accent); border-radius: 8px; background: var(--panel); box-shadow: 0 8px 28px #0008; color: var(--text); font-size: 12px; line-height: 1.5; pointer-events: none; }
  [role="tooltip"].above { transform: translateY(-100%); }
</style>
