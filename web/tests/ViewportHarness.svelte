<script lang="ts">
  import Viewport from "../src/components/Viewport.svelte";
  import TrimBar from "../src/components/TrimBar.svelte";
</script>
<Viewport width={3840} height={2160}>
  <div data-testid="picture">Video</div>
  {#snippet tools()}<button>Tool</button>{/snippet}
  {#snippet timeline()}<TrimBar edge index={20} count={100} start={20} end={80} rate={24} onseek={() => {}} />{/snippet}
  {#snippet controls()}<button>Play</button>{/snippet}
</Viewport>
