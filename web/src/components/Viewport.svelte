<script lang="ts">
  import { bridge } from "../lib/bridge";
  import { onMount, type Snippet } from "svelte";
  import Icon from "./Icon.svelte";
  import ViewMenu from "./ViewMenu.svelte";
  // The picture with a slim strip along its top edge (tools at the left; zoom and fullscreen at the right)
  // and the controls below it. The strip sits outside the picture, so the desktop app's native video
  // surface never covers it.
  // `ondock` offers moving the picture to where the clip list is, with the settings beside it; `docked`
  // says it has moved, and the button then offers moving it back.
  let { width = 1920, height = 1080, compact = false, docked = false, ondock, children, overlay, tools, timeline, controls }: {
    width?: number; height?: number; compact?: boolean; docked?: boolean; ondock?: () => void; children: Snippet;
    overlay?: Snippet; tools?: Snippet; timeline?: Snippet;
    controls: Snippet;
  } = $props();
  const ZOOMS = [{ value: "fit", label: "Fit" }, ...[25, 50, 75, 100].map(percent => ({ value: String(percent), label: percent === 100 ? "100% (actual size)" : `${percent}%` }))];
  const identity = $props.id();
  const id = `mcrawsome-viewport-${identity}`;
  let viewport: HTMLDivElement;
  let changing = false;
  let zoom = $state("fit");
  let fit = $derived(zoom === "fit");
  // Actual size is one image pixel per screen pixel, whatever the display scaling.
  let scale = $derived(fit ? 1 : Number(zoom) / 100 / (typeof window === "undefined" ? 1 : window.devicePixelRatio || 1));
  let fullscreen = $state(false);
  let problem = $state("");
  type FullDocument = Document & { webkitFullscreenElement?: Element; webkitExitFullscreen?: () => void };
  type FullElement = HTMLDivElement & { webkitRequestFullscreen?: () => void };
  const current = () => document.fullscreenElement ?? (document as FullDocument).webkitFullscreenElement;
  async function toggleFullscreen() {
    if (changing) return;
    changing = true;
    try {
      if (current() === viewport) {
        if (document.exitFullscreen) await document.exitFullscreen();
        else (document as FullDocument).webkitExitFullscreen?.();
      } else {
        try {
          if (viewport.requestFullscreen) await viewport.requestFullscreen();
          else if ((viewport as FullElement).webkitRequestFullscreen) (viewport as FullElement).webkitRequestFullscreen!();
          else throw new Error("Fullscreen is unavailable");
        } catch (error) {
          if (!$bridge?.api.request_fullscreen || !await $bridge.api.request_fullscreen(id)) throw error;
        }
      }
      problem = "";
    } catch { problem = "Could not change fullscreen mode"; }
    finally { changing = false; }
  }
  onMount(() => {
    const changed = () => { fullscreen = current() === viewport; if (fullscreen) problem = ""; };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && current() === viewport) {
        event.preventDefault();
        void toggleFullscreen();
      } else if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "f") {
        event.preventDefault();
        void toggleFullscreen();
      }
    };
    document.addEventListener("fullscreenchange", changed);
    document.addEventListener("webkitfullscreenchange", changed);
    document.addEventListener("keydown", escape);
    return () => {
      if (current() === viewport) {
        if (document.exitFullscreen) void document.exitFullscreen().catch(() => {});
        else (document as FullDocument).webkitExitFullscreen?.();
      }
      document.removeEventListener("fullscreenchange", changed);
      document.removeEventListener("webkitfullscreenchange", changed);
      document.removeEventListener("keydown", escape);
    };
  });
</script>

<div {id} data-mcrawsome-viewport="true" class="viewport" class:compact bind:this={viewport!} aria-label="Video viewport" style={`--aspect: ${Math.max(1, width)} / ${Math.max(1, height)}; --ratio: ${Math.max(1, width) / Math.max(1, height)}`}>
  <div class="strip">
    <div class="tools">{#if tools}{@render tools()}{/if}</div>
    <ViewMenu icon="zoom" label="Zoom" title="Zoom" align="right" shown={fit ? "Fit" : `${zoom}%`} items={ZOOMS} value={zoom} onselect={(value) => { zoom = value; }} />
    {#if ondock && !fullscreen}<button class="control" aria-label={docked ? "Move the preview back" : "Move the preview to the clip list"}
      title={docked ? "Put the preview back above the settings and show the clips" : "Show the preview where the clips are, with the settings beside it"}
      onclick={ondock}><Icon name={docked ? "undock" : "dock"} /></button>{/if}
    <button class="control" aria-label={fullscreen ? "Exit fullscreen" : "Enter fullscreen"} title={fullscreen ? "Exit fullscreen (Ctrl+F)" : "Enter fullscreen (Ctrl+F)"} onclick={() => void toggleFullscreen()}><Icon name={fullscreen ? "collapse" : "fullscreen"} /></button>
  </div>
  <div class="picture">
    <div class="image-clip"><div class="image-surface" class:fit style:width={fit ? "100%" : `${width * scale}px`} style:height={fit ? "100%" : `${height * scale}px`}>{@render children()}</div></div>
    {#if overlay}{@render overlay()}{/if}
  </div>
  <!-- Its own row, so the resting bar hides none of the picture (a crop outline's bottom edge included);
       expanded for trimming, it grows up over the picture. -->
  {#if timeline}<div class="timeline">{@render timeline()}</div>{/if}
  <div class="controls">{@render controls()}</div>
  {#if problem}<p role="status">{problem}</p>{/if}
</div>

<style>
  .viewport { box-sizing: border-box; width: min(100%, calc(65vh * var(--ratio))); margin-inline: auto; min-width: 0; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius); background: #080b0d; }
  /* In a pane the picture takes the video's shape up to a height cap, so a landscape clip fills the
     width; a portrait one is centred, as wide as its picture (with room for the controls). */
  .compact { --cap: var(--preview-cap, clamp(150px, 40dvh, 480px)); width: min(100%, max(22em, calc(var(--cap) * var(--ratio)))); }
  .compact:not(:fullscreen):not(:-webkit-full-screen) .picture { max-height: var(--cap); }
  .picture { position: relative; width: 100%; aspect-ratio: var(--aspect); min-height: 0; overflow: hidden; }
  .image-clip { position: absolute; inset: 0; overflow: auto; display: flex; }
  .image-surface { min-width: 0; flex: none; margin: auto; }
  .image-surface.fit { position: absolute; inset: 0; }
  .strip { display: flex; align-items: center; gap: 2px; min-height: 30px; padding: 3px 6px; background: var(--panel); }
  .tools { display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0; }
  .control { display: grid; place-items: center; min-width: 28px; height: 26px; padding: 3px; background: transparent; color: var(--text); border: 1px solid transparent; }
  .control:hover { background: var(--card); }
  .control :global(svg) { width: 17px; height: 17px; }
  .timeline { position: relative; z-index: 4; flex: none; height: 14px; background: var(--panel); }
  .controls { position: relative; flex: none; padding: 6px 8px; background: var(--panel); }
  p { margin: 0; padding: 8px 12px; color: var(--warn); font-size: 12px; }
  .viewport:fullscreen { display: flex; flex-direction: column; width: 100vw; height: 100vh; border: 0; border-radius: 0; }
  .viewport:fullscreen .picture { flex: 1; aspect-ratio: auto; max-height: none; min-height: 0; }
  .viewport:-webkit-full-screen { display: flex; flex-direction: column; width: 100vw; height: 100vh; border: 0; border-radius: 0; }
  .viewport:-webkit-full-screen .picture { flex: 1; aspect-ratio: auto; max-height: none; min-height: 0; }
</style>
