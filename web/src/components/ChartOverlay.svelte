<script lang="ts">
  import { overlayPath, overlayEnvelope, overlayProgress, type OverlayModel } from "../lib/preview-overlay";
  let { model, index }: { model: OverlayModel; index: number } = $props();
  import { isolatedPoints } from "../lib/analytics-chart";
  import { CHART_DASHES } from "../lib/analytics-catalogue";
  const identity = $props.id();
  let progress = $derived(overlayProgress(index, model.frames));
  let paths = $derived(model.lines.map(line => ({ ...line, path: overlayPath(line.points), envelope: overlayEnvelope(line.points), isolated: isolatedPoints(line.points, point => point.y) })));
</script>
<div class="chart-overlay" role="img" aria-label={`${model.title} preview overlay`} title={model.note}>
  <div class="caption"><strong>{model.title}</strong><span>{#if model.lines.length > 1}{#each model.lines as line, index}<span style:color={line.colour}><i style:border-top-style={index === 0 ? "solid" : index === 2 ? "dotted" : "dashed"}></i>{line.label}</span>{/each}{/if}</span></div>
  <svg class="plot" viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true">
    <defs><clipPath id={identity}><rect width={progress * 1000} height="100" /></clipPath></defs>
    {#each [0, 50, 100] as level}<line x1="0" x2="1000" y1={level} y2={level} stroke="white" stroke-opacity=".08" stroke-width="1" vector-effect="non-scaling-stroke" />{/each}
    {#each paths as line, index}
      {#each line.isolated as p}<circle cx={p.x * 1000} cy={p.y! * 100} r="2" fill={line.colour} fill-opacity=".22" />{/each}
      <path d={line.path} fill="none" stroke={line.colour} stroke-opacity=".22" stroke-width="1.8" stroke-dasharray={CHART_DASHES[index]} vector-effect="non-scaling-stroke" />
      <g clip-path={`url(#${identity})`}>
        <path d={line.envelope} fill={line.colour} fill-opacity=".14" />
        {#each line.isolated as p}<circle cx={p.x * 1000} cy={p.y! * 100} r="2" fill={line.colour} />{/each}
        {#each line.points as p}{#if p.y !== null && p.low !== null && p.high !== null}<line x1={p.x * 1000} x2={p.x * 1000} y1={p.low * 100} y2={p.high * 100} stroke={line.colour} stroke-opacity=".22" stroke-width="1" vector-effect="non-scaling-stroke" />{/if}{/each}
        <path d={line.path} fill="none" stroke={line.colour} stroke-width="1.8" stroke-dasharray={CHART_DASHES[index]} vector-effect="non-scaling-stroke" />
      </g>
    {/each}
    <line class="playhead" x1={progress * 1000} x2={progress * 1000} y1="0" y2="100" stroke="white" stroke-opacity=".8" stroke-width="1" vector-effect="non-scaling-stroke" />
  </svg>
</div>
<style>
  .chart-overlay { position: absolute; inset: auto 12px 12px; height: clamp(71px, calc(22% + 16px), 166px); pointer-events: none; padding: 8px 9px; border-radius: 8px; border: 1px solid rgb(255 255 255 / .12); background: rgb(8 17 24 / .84); box-shadow: 0 4px 16px #0003; color: #fff; display: flex; flex-direction: column; gap: 6px; }
  .caption { display: flex; justify-content: space-between; gap: 12px; font-size: 10px; line-height: 1.3; }
  strong { font-weight: 500; } .caption > span { display: flex; gap: 12px; } .caption > span > span { display: flex; gap: 4px; align-items: center; } .caption i { width: 16px; border-top: 2px solid currentColor; }
  .plot { width: 100%; flex: 1; min-height: 0; overflow: visible; }
</style>
