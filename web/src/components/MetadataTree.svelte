<script lang="ts">
  import MetadataTree from "./MetadataTree.svelte";
  import { metadataLabel, metadataValue, metadataHelp } from "../lib/metadata";
  let { values }: { values: Record<string, unknown> } = $props();
</script>
<dl aria-label="Recorded metadata">
  {#each Object.entries(values) as [key, value] (key)}
    {#if value !== null && value !== undefined}
      {#if typeof value === "object" && !Array.isArray(value)}
        <div class="nested"><dt title={metadataHelp(key)}>{metadataLabel(key)}</dt><dd><details><summary>View fields</summary><MetadataTree values={value as Record<string, unknown>} /></details></dd></div>
      {:else if Array.isArray(value) && (value.length > 12 || value.some(item => typeof item === "object"))}
        <div class="nested"><dt title={metadataHelp(key)}>{metadataLabel(key)}</dt><dd><details><summary>View {value.length} items</summary><pre>{metadataValue(value)}</pre></details></dd></div>
      {:else}
        <div class="row"><dt title={metadataHelp(key)}>{metadataLabel(key)}</dt><dd>{Array.isArray(value) ? value.map(metadataValue).join(", ") : metadataValue(value)}</dd></div>
      {/if}
    {/if}
  {/each}
</dl>
<style>
  dl { margin: 0; font-size: 12px; }
  .row { display: grid; grid-template-columns: minmax(100px, 40%) minmax(0, 1fr); gap: 8px; padding: 4px 0; }
  dt { color: var(--accent); overflow-wrap: anywhere; }
  dd { margin: 0; overflow-wrap: anywhere; user-select: text; }
  .nested { padding: 5px 0; border-top: 1px solid var(--line); }
  summary { cursor: pointer; color: var(--text); }
  details :global(dl) { margin: 8px 0 0 8px; }
  pre { max-height: 220px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; user-select: text; }

</style>
