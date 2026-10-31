<script lang="ts">
  import { BAYER_PLANES, gainMaps, noiseCurves } from "../lib/calibration-visuals";
  let { values }: { values: Record<string, unknown> } = $props();
  import { channelColour, heatmapColour } from "../lib/analytics-catalogue";
  import HeatmapScale from "./HeatmapScale.svelte";
  const colours = BAYER_PLANES.map(label => channelColour(label));
  let maps = $derived(gainMaps(values));
  let noise = $derived(noiseCurves(values));
</script>
{#if maps}
  <figure>
    <figcaption>Recorded lens shading</figcaption>
    <p>Shared gain scale: {maps.min.toFixed(3)} to {maps.max.toFixed(3)}x. Brighter cells mean stronger correction.{#if maps.sampled}{" "}Maps sampled to at most 24 x 24 cells.{/if}</p>
    <div class="planes">{#each maps.planes as plane, index}
      <div><span>{BAYER_PLANES[index]}</span><svg role="img" aria-label={`Lens shading ${BAYER_PLANES[index]}`} viewBox={`0 0 ${maps.width} ${maps.height}`} preserveAspectRatio="none">
        {#each plane as gain, cell}<rect x={cell % maps.width} y={Math.floor(cell / maps.width)} width="1" height="1" fill={heatmapColour(gain, maps.min, maps.max)}><title>{gain.toPrecision(6)}x</title></rect>{/each}
      </svg></div>
    {/each}</div>
    <HeatmapScale low={maps.min} high={maps.max} />
    <p>Sensor orientation. Exact map dimensions and gains are retained below.</p>
  </figure>
{/if}
{#if noise}
  <figure>
    <figcaption>Recorded noise model</figcaption>
    <svg role="img" aria-label="Recorded noise model" viewBox="0 0 320 145">
      <path d="M30,10 V110 H290" stroke="currentColor" fill="none" />
      <text x="30" y="8">{noise.max.toPrecision(3)}%</text><text x="6" y="113">0</text><text x="30" y="128">0</text><text x="280" y="128">1</text>
      <text x="85" y="143">Normalised sensor signal</text>
      {#each noise.paths as path, index}<path d={path} stroke={colours[index]} stroke-width="1.8" fill="none" />{/each}
    </svg>
    <div class="legend">{#each BAYER_PLANES as label, index}<span style:color={colours[index]}>{label}</span>{/each}</div>
    <p>Noise standard deviation (% of sensor range): sqrt(S x signal + O). This is the camera’s model for the first frame.</p>
  </figure>
{/if}
<style>
  figure { margin: 0 0 16px; padding: 10px; border: 1px solid var(--line); border-radius: 6px; }
  figcaption { font-weight: 600; font-size: 12px; } p { font-size: 11px; color: var(--muted); line-height: 1.5; }
  .planes { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .planes span { display: block; margin-bottom: 5px; font-size: 11px; }
  svg { width: 100%; color: var(--muted); } .planes svg { height: 100px; border-radius: 3px; }
  text { fill: currentColor; font-size: 9px; } .legend { display: flex; flex-wrap: wrap; justify-content: center; gap: 12px; font-size: 11px; }
</style>
