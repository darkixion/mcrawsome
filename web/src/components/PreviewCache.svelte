<script lang="ts">
  import { api, type Schemas } from "../lib/api";
  import type { PreviewClient } from "../lib/preview";
  let { client = null }: { client?: Pick<PreviewClient, "cacheUsage" | "clearFrameCache"> | null } = $props();
  let preview = $state<Schemas["PreviewCacheUsage"] | null>(null);
  let browser = $state<ReturnType<PreviewClient["cacheUsage"]> | null>(null);
  let busy = $state(false);
  let message = $state("");
  const mib = (bytes: number) => `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  async function refresh() {
    busy = true; message = "";
    const target = client;
    const result = await api.previewCacheUsage();
    if (target !== client) { preview = null; browser = null; message = "Preview changed; refresh cache usage and try again."; }
    else if (result.ok) { preview = result.data; browser = target?.cacheUsage() ?? null; }
    else { preview = null; browser = null; message = result.message; }
    busy = false;
  }
  async function clear() {
    busy = true; message = "";
    const target = client;
    const result = await api.clearPreviewCache();
    if (target !== client) { preview = null; browser = null; message = "Preview changed; refresh cache usage and try again."; }
    else if (result.ok) {
      target?.clearFrameCache();
      browser = target?.cacheUsage() ?? null;
      preview = result.data;
      message = result.data.active || target ? "Preview frame cache cleared. Playback and the current picture are preserved." : "No active preview.";
    } else message = result.message;
    busy = false;
  }
</script>
<details ontoggle={(event) => { if (event.currentTarget.open && !busy) void refresh(); }}>
  <summary>Preview cache</summary>
  <section aria-label="Live preview cache">
    {#if preview}
      <p><strong>Worker frame memory</strong> · {mib(preview.bytes)} · {preview.entries} cached entries</p>
      {#if preview.active}
        <p>Limit for the open clip: {mib(preview.limit_bytes)}. Linear frames: {mib(preview.linear_bytes)}. JPEGs: {mib(preview.jpeg_bytes)}.</p>
      {:else}<p>No active preview.</p>{/if}
    {/if}
    {#if browser}
      <p><strong>Browser image estimate</strong> · {mib(browser.estimated_bytes)} · {browser.entries} entries · limit {mib(browser.limit_bytes)}. {browser.reclaimable_entries} unused entries can be cleared.</p>
    {/if}
    <p>Worker figures count cached frame payloads. Browser figures include estimated decoded images, including the current picture and queued frames that clearing preserves. Working memory and display slots are separate. Frames can be cached again as you use the preview.</p>
    <div>
      <button class="secondary" disabled={busy} onclick={() => void refresh()}>Refresh preview cache usage</button>
      <button class="secondary" disabled={busy || (!(preview?.active && preview.entries > 0) && !browser?.reclaimable_entries)} onclick={() => void clear()}>Clear preview frames</button>
    </div>
    {#if message}<p role="status">{message}</p>{/if}
  </section>
</details>
<style>
  details { padding: 8px 0; font-size: 12px; color: var(--muted); }
  summary { cursor: pointer; color: var(--text); }
  p { margin: 6px 0; line-height: 1.5; }
  div { display: flex; gap: 8px; flex-wrap: wrap; }
  button { font-size: 12px; }
</style>
