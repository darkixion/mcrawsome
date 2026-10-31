<script lang="ts">
  import { settings } from "../lib/settings";
  import InfoTip from "./InfoTip.svelte";
  let { width, height, raw = true }: { width?: number | null; height?: number | null; raw?: boolean } = $props();
</script>
{#if raw && !$settings?.resolve_full_resolution && width != null && height != null && (width > 3840 || height > 2160)}
  <InfoTip warning label="Resolve timeline resolution limit" text="The free version of DaVinci Resolve does not support timeline resolutions above 3840 x 2160, so the timeline will be limited to those dimensions. The timeline uses the largest fitting resolution for the clip’s proportions. Media Bin sends and the media inside timelines keep their full mounted resolution. Choose DaVinci Resolve Studio in Settings to use full source resolution without this limit." />
{/if}
