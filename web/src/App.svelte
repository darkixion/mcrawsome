<script lang="ts">
  import { onMount } from "svelte";
  import EngineStatus from "./components/EngineStatus.svelte";
  import IconRail from "./components/IconRail.svelte";
  import { hasUnsavedAnnotations } from "./lib/annotations";
  import { api, type ApiError } from "./lib/api";
  import ErrorNote from "./components/ErrorNote.svelte";
  import { connectDesktopOpen } from "./lib/desktop-open";
  import { connect } from "./lib/events";
  import { route } from "./lib/route";
  import { settings } from "./lib/settings";
  import Library from "./screens/Library.svelte";
  import About from "./screens/About.svelte";
  import Settings from "./screens/Settings.svelte";

  let openProblem = $state<ApiError | null>(null);

  onMount(() => {
    void api.settings().then((loaded) => {
      if (loaded.ok) settings.set(loaded.data);
    });
    const stop = connect();
    const stopOpen = connectDesktopOpen(problem => { openProblem = problem; });
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (hasUnsavedAnnotations()) { event.preventDefault(); event.returnValue = ""; }
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => { stopOpen(); stop(); window.removeEventListener("beforeunload", beforeUnload); };
  });
</script>

<div class="shell">
  <IconRail route={$route.screen} />
  <div class="main">
    <EngineStatus />
    <main>
      {#if openProblem}<ErrorNote error={openProblem} />{/if}
      {#if $route.screen === "library"}
        <Library />
      {:else if $route.screen === "about"}
        <About />
      {:else if $route.screen === "settings"}
        <Settings />
      {/if}
    </main>
  </div>
</div>

<style>
  .shell {
    display: flex;
    height: 100vh;
  }
  .main {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-width: 0;
  }
  main {
    flex: 1;
    overflow: auto;
  }
</style>
