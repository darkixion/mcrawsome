<script lang="ts">
  import { CHART, chartTick, chartNumber, chartValue, chartPath, chartEnvelope, chartOutliers, isolatedPoints, nearestSample, niceScale, type ChartLine } from "../lib/analytics-chart";
  import { CHART_DASHES } from "../lib/analytics-catalogue";
  const identity = $props.id();
  let { label, lines, xMin, xMax, xLabel = "Recorded frame", xUnit = "frame", yLabel, minimum, maximum, reference = null, summary = "Grouped mean", highlightIntervals = false }: {
    label: string; lines: ChartLine[]; xMin: number; xMax: number; xLabel?: string; xUnit?: "frame" | "seconds" | "code";
    yLabel: string; minimum?: number; maximum?: number; reference?: number | null; summary?: string; highlightIntervals?: boolean;
  } = $props();
  let svg: SVGSVGElement;
  let hovered = $state<number | null>(null);
  let visible = $derived(lines.filter(line => line.visible !== false));
  let samples = $derived(lines[0]?.samples ?? []);
  let values = $derived(visible.flatMap(line => line.samples.flatMap(point => [point.value, point.minimum, point.maximum].filter((value): value is number => value != null && Number.isFinite(value)))));
  let scale = $derived(niceScale(minimum ?? (values.length ? Math.min(...values) : 0), maximum ?? (values.length ? Math.max(...values) : 1)));
  let x = $derived((value: number) => CHART.left + (value - xMin) / Math.max(xMax - xMin, 1e-12) * (CHART.right - CHART.left));
  let y = $derived((value: number) => CHART.bottom - (value - scale.low) / (scale.high - scale.low) * (CHART.bottom - CHART.top));
  let ticks = $derived(xMax <= xMin ? [xMin] : [xMin, ...niceScale(xMin, xMax).ticks.filter(value => value > xMin + (xMax - xMin) * .04 && value <= xMax)
    .filter(value => xUnit === "seconds" || Number.isInteger(value))]);
  let point = $derived(hovered == null ? null : samples[Math.min(hovered, samples.length - 1)]);
  let readouts = $derived(point ? visible.map(line => ({ line, point: line.samples[nearestSample(line.samples, point!.x)] })) : []);
  let cursor = $derived(point ? x(point.x) : 0);
  let paths = $derived(visible.map(line => ({ line, dash: CHART_DASHES[lines.indexOf(line) % CHART_DASHES.length], path: chartPath(line.samples, x, y), envelope: chartEnvelope(line.samples, x, y), isolated: isolatedPoints(line.samples, point => point.value), outliers: highlightIntervals ? chartOutliers(line.samples, reference) : [],
    low: Math.min(...line.samples.flatMap(point => [point.value, point.minimum].filter((v): v is number => v != null && Number.isFinite(v)))),
    high: Math.max(...line.samples.flatMap(point => [point.value, point.maximum].filter((v): v is number => v != null && Number.isFinite(v)))) })));
  const tickLabel = (value: number) => xUnit === "seconds" ? `${chartNumber(value)} s` : chartNumber(value);
  function inspect(event: PointerEvent): void {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const px = (event.clientX - rect.left) / rect.width * CHART.width;
    const position = xMin + (px - CHART.left) / (CHART.right - CHART.left) * (xMax - xMin);
    hovered = nearestSample(samples, Math.max(xMin, Math.min(xMax, position)));
  }
  function keyboard(event: KeyboardEvent): void {
    if (!["ArrowLeft", "ArrowRight", "Home", "End", "Escape"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Escape") { hovered = null; return; }
    hovered = event.key === "Home" ? 0 : event.key === "End" ? samples.length - 1 : Math.max(0, Math.min(samples.length - 1, (hovered ?? 0) + (event.key === "ArrowLeft" ? -1 : 1)));
  }
</script>
<section class="chart-card" aria-label={`${label} chart`}>
  <header><div><span class="eyebrow">RECORDED MEASUREMENTS</span><h4>{label}</h4></div><span class="measure">{yLabel}</span></header>
  <ul class="legend" aria-label={`${label} legend`}>
    {#each paths as item}<li><div><svg viewBox="0 0 24 8" aria-hidden="true"><path d="M1 4 H23" stroke={item.line.colour} stroke-width="2" stroke-dasharray={item.dash} /></svg><span>{item.line.label}</span></div>
      <b>{Number.isFinite(item.low) ? `${chartNumber(item.low)}–${chartNumber(item.high)}` : "Unavailable"}</b><small>{item.line.unit} · min / max</small></li>{/each}
  </ul>
<div class="chart" role="slider" tabindex="0" aria-label={`Inspect ${label}`} aria-valuemin={xMin} aria-valuemax={Math.max(xMin, xMax)} aria-valuenow={point?.x ?? xMin}
  aria-valuetext={point ? `${tickLabel(point.x)}, ${readouts.map(row => `${row.line.label} ${row.point?.value == null ? "Unavailable" : chartValue(row.point.value)} ${row.line.unit}`).join(", ")}` : "Hover or use arrow keys to inspect values"}
  onpointermove={inspect} onpointerleave={() => hovered = null} onblur={() => hovered = null} onfocus={() => { if (hovered == null && samples.length) hovered = 0; }} onkeydown={keyboard}>
  <svg bind:this={svg} role="img" aria-label={label} viewBox={`0 0 ${CHART.width} ${CHART.height}`}>
    <defs>{#each paths as item, index}<linearGradient id={`${identity}-band-${index}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color={item.line.colour} stop-opacity=".22" /><stop offset="100%" stop-color={item.line.colour} stop-opacity=".04" /></linearGradient>{/each}</defs>
    <rect class="plot-surface" x={CHART.left} y={CHART.top} width={CHART.right - CHART.left} height={CHART.bottom - CHART.top} rx="4" />
    {#each scale.ticks as tick}<line class="grid" x1={CHART.left} x2={CHART.right} y1={y(tick)} y2={y(tick)} /><text class="y-tick" x={CHART.left - 9} y={y(tick) + 3}>{chartTick(tick, scale.step)}</text>{/each}
    {#each ticks as tick}<line class="grid vertical" x1={x(tick)} x2={x(tick)} y1={CHART.top} y2={CHART.bottom} /><text class="x-tick" x={x(tick)} y={CHART.bottom + 18}>{tickLabel(tick)}</text>{/each}
    <text class="unit" x={CHART.left} y="10">{yLabel}</text><text class="x-title" x={(CHART.left + CHART.right) / 2} y="222">{xLabel}</text>
    {#if reference != null && reference >= scale.low && reference <= scale.high}<line x1={CHART.left} x2={CHART.right} y1={y(reference)} y2={y(reference)} class="reference" />{/if}
    {#each paths as item, index}
      <path class="band" d={item.envelope} fill={`url(#${identity}-band-${index})`} />
      {#each item.line.samples as sample}{#if sample.value != null && sample.minimum != null && sample.maximum != null}<line x1={x(sample.x)} x2={x(sample.x)} y1={y(sample.minimum)} y2={y(sample.maximum)} stroke={item.line.colour} class="envelope" />{/if}{/each}
      <path class="trace" d={item.path} stroke={item.line.colour} fill="none" stroke-width="1.8" stroke-dasharray={item.dash} />
      {#each item.outliers as marker}<path class="outlier" d={`M${x(marker.x)},${y(marker.value) - 4} l4,4 l-4,4 l-4,-4 Z`}><title>{`${marker.kind}: ${chartValue(marker.value)} ${item.line.unit}`}</title></path>{/each}
      {#each item.isolated as sample}<circle class="isolated" cx={x(sample.x)} cy={y(sample.value!)} r="2.5" fill={item.line.colour} />{/each}
    {/each}
    {#if point}
      <line class="cursor" x1={cursor} x2={cursor} y1={CHART.top} y2={CHART.bottom} />
      {#each readouts as row}{#if row.point?.value != null}<circle cx={cursor} cy={y(row.point.value)} r="3" fill={row.line.colour} stroke="var(--panel)" />{/if}{/each}
    {/if}
  </svg>
  {#if point}
    <div class="tooltip" style:left={`clamp(8px, calc(${cursor / CHART.width * 100}% - 145px), calc(100% - 298px))`}>
      <strong>{xUnit === "seconds" ? `Time ${chartNumber(point.x)} s` : xUnit === "code" ? `RAW code ${chartNumber(point.x)}` : `Frame ${point.frame ?? point.x}`}</strong>
      {#if xUnit !== "seconds" && point.time != null}<span>Time {chartValue(point.time)} s</span>{/if}
      {#if xUnit === "seconds"}<span>{point.frame != null ? `Recorded frame ${point.frame}` : "Frame link unavailable here"}</span>{/if}
      {#if point.firstFrame != null && point.lastFrame != null && point.firstFrame !== point.lastFrame}<span>Frames {point.firstFrame}–{point.lastFrame}</span>{/if}
      <small>{summary}</small>
      {#each readouts as row}<div class="reading"><i style:background={row.line.colour}></i><span>{row.line.label}</span><b>{row.point?.value == null ? "Unavailable" : chartValue(row.point.value)} {row.line.unit}</b></div>
        {#if row.point?.minimum != null && row.point.maximum != null}<small class="range">Range {chartValue(row.point.minimum)}–{chartValue(row.point.maximum)}</small>{/if}
      {/each}
    </div>
  {/if}
</div>
<footer><span><i class="range-key"></i> Min / max band · line shows mean</span>{#if highlightIntervals}<span class="marker-key">◇ Unusual interval</span>{/if}<span>Inspect: hover · ← / →</span></footer>
</section>
<style>
  .chart-card { margin: 14px 0; padding: 16px 14px 10px; min-width: 0; border: 1px solid var(--line); border-radius: 10px; background: linear-gradient(145deg, var(--card), var(--panel) 65%); box-shadow: 0 4px 16px #0002; }
  header { display: flex; gap: 12px; justify-content: space-between; align-items: start; }
  .eyebrow { color: var(--muted); font-size: 8px; letter-spacing: .13em; font-weight: 600; }
  h4 { margin: 4px 0 0; font-size: 14px; font-weight: 500; color: var(--text); }
  .measure { font-size: 10px; color: var(--accent); text-align: right; padding-top: 3px; }
  .legend { display: flex; flex-wrap: wrap; gap: 14px 20px; padding: 0; margin: 16px 0 10px; list-style: none; }
  .legend li { min-width: 0; flex: 1 1 90px; font-size: 10px; }
  .legend li > div { display: flex; align-items: center; gap: 5px; color: var(--muted); }
  .legend svg { width: 24px; height: 8px; flex-shrink: 0; }
  .legend b { display: block; margin: 4px 0 2px; font-size: 12px; font-weight: 500; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
  .legend small { color: var(--muted); font-size: 9px; }
  .chart { position: relative; margin-top: 18px; min-width: 0; border-radius: 6px; touch-action: pan-y; }
  .chart:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
  svg { width: 100%; display: block; overflow: visible; }
  text { fill: var(--muted); font-size: 10px; font-variant-numeric: tabular-nums; }
  .plot-surface { fill: var(--bg); fill-opacity: .45; }
  .y-tick { text-anchor: end; } .x-tick, .x-title { text-anchor: middle; } .unit { fill: var(--muted); font-size: 9px; }
  .grid { stroke: var(--line); stroke-width: .7; } .vertical { stroke-dasharray: 2 5; opacity: .45; }
  .reference { stroke: var(--muted); stroke-dasharray: 4 4; opacity: .7; }
  .trace { stroke-linejoin: round; } .envelope { stroke-width: 1; opacity: .2; }
  .outlier { fill: var(--panel); stroke: var(--warn); stroke-width: 1.5; }
  .cursor { stroke: var(--text); stroke-width: 1; stroke-dasharray: 3 3; }
  .tooltip { position: absolute; top: 18px; z-index: 2; pointer-events: none; display: grid; gap: 5px; width: 290px; max-width: calc(100% - 16px); padding: 12px 14px; border: 1px solid var(--accent); border-radius: 8px; background: var(--panel); box-shadow: 0 8px 28px #0008; color: var(--text); font-size: 11px; font-variant-numeric: tabular-nums; }
  .tooltip strong { color: var(--accent); white-space: nowrap; }
  .tooltip small, .tooltip > span { color: var(--muted); font-size: 10px; }
  .reading { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; } .reading b { font-weight: 500; margin-left: auto; } .reading i { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .range { padding-left: 12px; }
  footer { display: flex; flex-wrap: wrap; gap: 6px 12px; justify-content: space-between; border-top: 1px solid var(--line); padding-top: 9px; color: var(--muted); font-size: 9px; }
  footer span { display: flex; align-items: center; gap: 5px; } .range-key { width: 12px; height: 7px; background: var(--accent); opacity: .35; } .marker-key { color: var(--warn); }
  @media (prefers-reduced-motion: no-preference) { .band, .trace { animation: appear .3s ease-out; } @keyframes appear { from { opacity: 0; } to { opacity: 1; } } }
  @container (max-width: 380px) { .chart-card { padding: 12px 8px 9px; } .legend { gap: 10px; } }
</style>
