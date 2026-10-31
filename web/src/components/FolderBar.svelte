<script lang="ts">
  // The Library's folders as chips; one the engine cannot find (an unplugged drive) says so.
  let { folders, missing }: { folders: string[]; missing: string[] } = $props();
  const same = (path: string): string => path.replace(/(?<=.)[\\/]+$/, "");
  let gone = $derived(new Set(missing.map(same)));
  const name = (path: string): string => same(path).split(/[\\/]/).at(-1) || path;
</script>

<ul aria-label="Folders">
  {#each folders as folder (folder)}
    <li title={folder} class:missing={gone.has(same(folder))}>
      <span>{name(folder)}</span>
      {#if gone.has(same(folder))}<span class="note">not found</span>{/if}
    </li>
  {/each}
</ul>

<style>
  ul {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  li {
    display: flex;
    gap: 6px;
    padding: 2px 10px;
    border: 1px solid var(--line);
    border-radius: 999px;
    background: var(--panel);
    font-size: 12px;
  }
  .missing {
    border-color: var(--warn);
  }
  .note {
    color: var(--warn);
  }
</style>
