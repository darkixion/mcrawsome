<script lang="ts">
  import { bridge } from "../lib/bridge";
  import { engine } from "../lib/events";

  let problem = $state("");
  let restarting = $state(false);
  async function restart(): Promise<void> {
    restarting = true;
    problem = "";
    try {
      await $bridge?.api.restart_engine();
      window.location.reload();
    } catch {
      problem = "Could not restart the engine";
    } finally {
      restarting = false;
    }
  }

  const LABELS = { reconnecting: "Reconnecting to the engine", stopped: "Engine stopped" };

  // Shown only when something is wrong: at once when the engine has stopped, and when reconnecting only
  // after a moment, so that the window's first connection does not flash it.
  let slow = $state(false);
  $effect(() => {
    if ($engine !== "reconnecting") {
      slow = false;
      return;
    }
    const timer = setTimeout(() => { slow = true; }, 2000);
    return () => clearTimeout(timer);
  });
</script>

{#if $engine === "stopped" || ($engine === "reconnecting" && slow)}
  <div class="status {$engine}" role="status">
    <span class="dot" aria-hidden="true"></span>
    {LABELS[$engine]}
    {#if $engine === "stopped"}
      {#if $bridge?.api.restart_engine}
        <button type="button" disabled={restarting} onclick={() => void restart()}>Restart</button>
      {:else}
        <span>Run <code>mcrawsome server --port {window.location.port || "8642"}</code></span>
      {/if}
      {#if problem}<span>{problem}</span>{/if}
    {/if}
  </div>
{/if}

<style>
  .status {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 16px;
    border-bottom: 1px solid var(--line);
    background: var(--panel);
    color: var(--muted);
    font-size: 12px;
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }
  .reconnecting .dot {
    background: var(--warn);
  }
  .stopped {
    color: var(--text);
  }
  .stopped .dot {
    background: var(--fail);
  }
</style>
