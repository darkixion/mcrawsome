<script lang="ts">
  import { api, type Schemas } from "../lib/api";
  import PreviewCache from "./PreviewCache.svelte";
  let entries = $state<Schemas["CacheUsage"][] | null>(null);
  let busy = $state(false);
  let message = $state("");
  const mib = (bytes: number) => `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  async function refresh() {
    busy = true; message = "";
    const result = await api.cacheUsage();
    if (result.ok) entries = result.data; else message = result.message;
    busy = false;
  }
  async function clear(kind: string) {
    if (kind !== "analysis" && kind !== "optical-tracks" && kind !== "exposure") return;
    busy = true; message = "";
    const result = await api.clearCache(kind);
    if (result.ok) {
      entries = entries?.map(item => item.kind === kind ? result.data : item) ?? null;
      message = "Completed cache entries cleared. Active media and playback are preserved.";
    } else message = result.message;
    busy = false;
  }
</script>
<section aria-label="Cache management">
  <h2>Cache management</h2>
  <p>Clear cached preview frames and completed analysis, optical tracking and exposure measurements. Mounted media, profiles, sessions and recordings are preserved. Audio, thumbnail readers and session plans remain protected.</p>
  <PreviewCache />
  <button class="secondary" disabled={busy} onclick={() => void refresh()}>{entries ? "Refresh cache usage" : "Show cache usage"}</button>
  {#if message}<p role="status">{message}</p>{/if}
  {#if entries}
    {#each entries as item}
      <div class="cache-row">
        <div><strong>{item.kind}</strong> · {mib(item.bytes)} · {item.files} completed files
          <p>{item.note} {item.limit_bytes == null ? "No aggregate disk limit." : `Limit: ${mib(item.limit_bytes)}.`}</p>
          <code>{item.path}</code>
        </div>
        {#if item.reclaimable}<button class="secondary" disabled={busy || item.files === 0} onclick={() => void clear(item.kind)}>Clear {item.kind}</button>{:else}<span>Protected</span>{/if}
      </div>
    {/each}
  {/if}
</section>
<style>
  section { padding: 20px; border: 1px solid var(--line); border-radius: 8px; margin-top: 20px; }
  h2 { font-size: 14px; } p, code, span { color: var(--muted); font-size: 12px; line-height: 1.5; }
  code { overflow-wrap: anywhere; } .cache-row { display: flex; justify-content: space-between; gap: 16px; border-top: 1px solid var(--line); padding: 12px 0; }
  .cache-row > div { min-width: 0; } button { flex-shrink: 0; align-self: center; }
</style>
