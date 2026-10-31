<script lang="ts">
  import ViewMenu from "../src/components/ViewMenu.svelte";
  let { viewer = true }: { viewer?: boolean } = $props();
  let value = $state("fit");
  const items = [{ value: "fit", label: "Fit" }, { value: "50", label: "50%" }, { value: "100", label: "100% (actual size)" }];
</script>
{#if viewer}
  <div data-mcrawsome-viewport="true" data-testid="viewer"><ViewMenu icon="zoom" label="Zoom" title="Zoom" align="right" {items} {value} onselect={(next) => value = next} /></div>
{:else}
  <ViewMenu icon="zoom" label="Zoom" title="Zoom" align="right" {items} {value} onselect={(next) => value = next} />
{/if}
