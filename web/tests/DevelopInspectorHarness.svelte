<script lang="ts">
  // Develop as the Library's right pane embeds it: compact, with its settings in the inspector.
  import Develop from "../src/screens/Develop.svelte";
  let { id, onplayback, onflush }: {
    id: string; onplayback?: (value: boolean) => void; onflush?: (flush: (() => Promise<boolean>) | null) => void;
  } = $props();
</script>
<Develop {id} {onplayback} {onflush}>
  {#snippet inspector(develop, savingText)}<aside><span>{savingText}</span>{@render develop()}</aside>{/snippet}
</Develop>
