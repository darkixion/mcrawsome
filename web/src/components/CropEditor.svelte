<script lang="ts">
  import type { Box } from "../lib/dng-crop";
  // Places the mounted DNG crop on the whole shown picture: drag inside to move it, drag an edge or
  // corner to resize it. `width` x `height` is the shown picture's full size, and `box` is in those pixels.
  // `onchange` gets the unsnapped rectangle; the caller snaps it to valid sensor pixels. In the desktop app
  // the native picture draws the rectangle (viewport.py) and the pointer still reaches this layer.
  let { width, height, box, aspect = null, onchange }: {
    width: number; height: number; box: Box; aspect?: number | null; onchange: (box: Box) => void;
  } = $props();
  type Mode = "move" | "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
  const GRAB = 10; // screen pixels around an edge that pick it up
  let svg: SVGSVGElement | undefined = $state();
  let hover = $state<Mode | null>(null);
  let shownScale = $state(0); // screen pixels per picture pixel
  let knob = $derived(shownScale ? 9 / shownScale : Math.max(width, height) / 60); // handles about 9 screen pixels
  $effect(() => {
    if (!svg || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => { shownScale = frame().scale; });
    observer.observe(svg);
    return () => observer.disconnect();
  });
  const CURSORS: Record<Mode, string> = { move: "move", n: "ns-resize", s: "ns-resize", e: "ew-resize", w: "ew-resize", ne: "nesw-resize", sw: "nesw-resize", nw: "nwse-resize", se: "nwse-resize" };

  function frame() {
    const area = svg!.getBoundingClientRect();
    const scale = Math.min(area.width / width, area.height / height);
    return { scale, left: area.left + (area.width - width * scale) / 2, top: area.top + (area.height - height * scale) / 2 };
  }
  function point(event: PointerEvent) {
    const { scale, left, top } = frame();
    return { x: (event.clientX - left) / scale, y: (event.clientY - top) / scale, grab: GRAB / scale };
  }
  function mode(event: PointerEvent): Mode | null {
    const { x, y, grab } = point(event);
    const right = box.left + box.width, bottom = box.top + box.height;
    const near = (value: number, edge: number) => Math.abs(value - edge) <= grab;
    const across = x >= box.left - grab && x <= right + grab, down = y >= box.top - grab && y <= bottom + grab;
    const v = across && near(y, box.top) ? "n" : across && near(y, bottom) ? "s" : "";
    const h = down && near(x, box.left) ? "w" : down && near(x, right) ? "e" : "";
    if (v || h) return (v + h) as Mode;
    return x > box.left && x < right && y > box.top && y < bottom ? "move" : null;
  }
  function resize(start: Box, picked: Mode, dx: number, dy: number): Box {
    let { left, top } = start, right = start.left + start.width, bottom = start.top + start.height;
    if (picked.includes("w")) left = Math.min(right - 2, Math.max(0, left + dx));
    if (picked.includes("e")) right = Math.max(left + 2, Math.min(width, right + dx));
    if (picked.includes("n")) top = Math.min(bottom - 2, Math.max(0, top + dy));
    if (picked.includes("s")) bottom = Math.max(top + 2, Math.min(height, bottom + dy));
    let next = { left, top, width: right - left, height: bottom - top };
    if (aspect) {
      // The dragged side leads; the other follows from the shape, anchored at the opposite side or centre.
      const byWidth = picked === "e" || picked === "w" || (picked.length === 2 && Math.abs(dx) >= Math.abs(dy));
      let w = byWidth ? next.width : next.height * aspect, h = w / aspect;
      const limit = Math.min(1, (picked.includes("w") ? right : width - left) / w, (picked.includes("n") ? bottom : height - top) / h);
      w *= limit; h *= limit;
      const x = picked.includes("w") ? right - w : picked.includes("e") ? left : start.left + (start.width - w) / 2;
      const y = picked.includes("n") ? bottom - h : picked.includes("s") ? top : start.top + (start.height - h) / 2;
      next = { left: Math.max(0, Math.min(width - w, x)), top: Math.max(0, Math.min(height - h, y)), width: w, height: h };
    }
    return next;
  }
  function down(event: PointerEvent): void {
    const picked = mode(event);
    if (!picked || event.button !== 0) return;
    const start = { ...box }, from = point(event);
    svg!.setPointerCapture(event.pointerId);
    const move = (next: PointerEvent) => {
      const at = point(next), dx = at.x - from.x, dy = at.y - from.y;
      onchange(picked === "move"
        ? { ...start, left: Math.max(0, Math.min(width - start.width, start.left + dx)), top: Math.max(0, Math.min(height - start.height, start.top + dy)) }
        : resize(start, picked, dx, dy));
    };
    const end = () => { svg!.removeEventListener("pointermove", move); svg!.removeEventListener("pointerup", end); svg!.removeEventListener("pointercancel", end); };
    svg!.addEventListener("pointermove", move);
    svg!.addEventListener("pointerup", end);
    svg!.addEventListener("pointercancel", end);
    event.preventDefault();
  }
</script>
<svg bind:this={svg} class="crop-editor" style:cursor={hover ? CURSORS[hover] : "default"} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet"
  role="img" aria-label={`Mounted DNG crop: ${Math.round(box.width)} by ${Math.round(box.height)} at ${Math.round(box.left)}, ${Math.round(box.top)}`}
  onpointerdown={down} onpointermove={(event) => hover = mode(event)} onpointerleave={() => hover = null}>
  <path fill-rule="evenodd" d={`M0 0H${width}V${height}H0Z M${box.left} ${box.top}h${box.width}v${box.height}h${-box.width}Z`} />
  <rect class="line" x={box.left} y={box.top} width={box.width} height={box.height} vector-effect="non-scaling-stroke" />
  {#each [0, 0.5, 1] as fx}{#each [0, 0.5, 1] as fy}{#if fx !== 0.5 || fy !== 0.5}
    <rect class="handle" x={box.left + fx * box.width - knob / 2} y={box.top + fy * box.height - knob / 2} width={knob} height={knob} />
  {/if}{/each}{/each}
</svg>
<style>
  .crop-editor { position: absolute; inset: 0; width: 100%; height: 100%; touch-action: none; }
  path { fill: #000; opacity: .55; }
  .handle { fill: #fff; }
  .line { fill: none; stroke: #fff; stroke-width: 2; stroke-dasharray: 6 4; }
</style>
