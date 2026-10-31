<script lang="ts">
  import type { Screen } from "../lib/route";
  import Icon from "./Icon.svelte";

  let { route }: { route: Screen } = $props();

  const main: { screen: Screen; name: string }[] = [
    { screen: "library", name: "Library" },
  ];
</script>

{#snippet item(screen: Screen, name: string)}
  <a href="#/{screen}" title={name} aria-label={name}
    aria-current={route === screen ? "page" : undefined} class:current={route === screen}>
    <Icon name={screen} />
  </a>
{/snippet}

<nav aria-label="Screens">
  {#each main as entry (entry.screen)}
    {@render item(entry.screen, entry.name)}
  {/each}
  <span class="gap"></span>
  {@render item("settings", "Settings")}
  {@render item("about", "About")}
</nav>

<style>
  nav {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    width: var(--rail);
    padding: 10px 0;
    background: var(--panel);
    border-right: 1px solid var(--line);
  }
  .gap {
    flex: 1;
  }
  a {
    position: relative;
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    border-radius: 8px;
    color: var(--muted);
  }
  a:hover {
    color: var(--text);
    background: var(--card);
  }
  a.current {
    color: var(--accent-ink);
    background: var(--accent);
  }
</style>
