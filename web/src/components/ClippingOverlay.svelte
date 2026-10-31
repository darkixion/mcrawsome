<script lang="ts">
  import { api, type RecipeValues } from "../lib/api";
  let { id, recipe, index, mode, active }: { id: string; recipe: RecipeValues; index: number; mode: "off" | "any" | "all"; active: boolean } = $props();
  let url = $state("");
  let problem = $state("");
  $effect(() => {
    url = ""; problem = "";
    if (!active || mode === "off" || index < 0) return;
    const controller = new AbortController();
    const asked = index, askedId = id, askedRecipe = recipe, askedMode = mode;
    async function load() {
      for (let attempt = 0; attempt < 4; attempt++) {
        const result = await api.sensorClipping(askedId, askedRecipe, asked, askedMode, controller.signal);
        if (controller.signal.aborted) return;
        if (result.ok) { if (result.data.frame_index === asked) url = result.data.url; return; }
        if (result.code !== "engine-busy" || attempt === 3) { problem = result.message; return; }
        await new Promise(resolve => setTimeout(resolve, 150));
        if (controller.signal.aborted) return;
      }
    }
    const timer = setTimeout(() => { void load(); }, 150);
    return () => { clearTimeout(timer); controller.abort(); };
  });
</script>
{#if active && url}<img class="clipping" src={url} alt="Pink sensor-clipping overlay" />{/if}
{#if active && problem}<span class="problem" role="status">{problem}</span>{/if}
<style>
  .clipping { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; pointer-events: none; }
  .problem { position: absolute; top: 8px; left: 8px; color: white; background: #171d25dd; padding: 4px; font-size: 11px; }
</style>
