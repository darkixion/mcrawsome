<script lang="ts">
  import Viewport from "../src/components/Viewport.svelte";
  let { ondock }: { ondock?: () => void } = $props();
  let docked = $state(false);
</script>
<Viewport width={3840} height={2160} compact {docked} ondock={ondock ? () => { docked = !docked; ondock(); } : undefined}>
  <div>Video</div>
  {#snippet controls()}<button>Play</button>{/snippet}
</Viewport>
