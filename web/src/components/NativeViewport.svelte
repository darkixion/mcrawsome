<script lang="ts">
  import { onMount, type Snippet } from "svelte";
  import { bridge } from "../lib/bridge";
  import { attachNative, type CropOutline, type NativeApi } from "../lib/native-preview";
  import { obscuring } from "../lib/obscuring";
  import type { OverlayModel } from "../lib/preview-overlay";
  import type { PreviewClient } from "../lib/preview";
  // `crop` outlines part of the picture (the stabilised frame, or a DNG crop being placed); the native
  // surface covers the page, so it draws the outline itself.
  let { client, overlay = null, crop = null, diagnostic = false, children }: { client: PreviewClient | null; overlay?: OverlayModel | null; crop?: CropOutline | null; diagnostic?: boolean; children: Snippet } = $props();
  let surface: HTMLDivElement;
  let mounted = $state(false);
  let connection: ReturnType<typeof attachNative> | null = null;
  onMount(() => { mounted = true; });
  $effect(() => {
    const api = $bridge?.api;
    if (diagnostic || !mounted || !client || !api?.preview_available || !api.preview_present || !api.preview_bounds || !api.preview_hide) return;
    if (!api.preview_overlay && overlay) return;
    const attached = attachNative(client, surface, api as NativeApi, () => overlay, () => crop);
    connection = attached;
    const observer = new ResizeObserver(attached.move);
    observer.observe(surface);
    window.addEventListener("resize", attached.move);
    window.addEventListener("scroll", attached.move, true);
    document.addEventListener("fullscreenchange", attached.move);
    document.addEventListener("webkitfullscreenchange", attached.move);
    const popups = obscuring.subscribe(() => attached.move());
    return () => {
      popups();
      attached.dispose(); observer.disconnect(); connection = null;
      window.removeEventListener("resize", attached.move);
      window.removeEventListener("scroll", attached.move, true);
      document.removeEventListener("fullscreenchange", attached.move);
      document.removeEventListener("webkitfullscreenchange", attached.move);
    };
  });
  $effect(() => { void overlay; connection?.chart(); });
  $effect(() => { void crop; connection?.guide(); });
</script>
<div bind:this={surface!}>{@render children()}</div>
<style>div { position: relative; width: 100%; height: 100%; }</style>
