<script lang="ts">
  import { contrastText } from "../lib/contrast";
  import Icon, { type IconName } from "./Icon.svelte";
  // A short label on a clip: its kind (RAW, LOG) or one of the engine's badges, with an optional icon.
  let { label, tone = "plain", icon }: { label: string; tone?: "plain" | "accent" | "warn" | "success" | "danger" | "violet" | "sky"; icon?: IconName } = $props();
  let badge = $state<HTMLSpanElement>();
  let ink = $state("#000");
  $effect(() => { void tone; if (badge) ink = contrastText(getComputedStyle(badge).backgroundColor) ?? (tone === "plain" ? "#fff" : "#000"); });
</script>

<span bind:this={badge} class="badge {tone}" class:with-icon={icon} style:color={ink}>{#if icon}<Icon name={icon} />{/if}{label}</span>

<style>
  /* Every chip is one height, whether it sits on its own or inside a span that carries its tooltip: in a
     row that stretches its items, Mounted grew taller than Idle beside it. Widths follow the labels. */
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    box-sizing: border-box;
    height: 20px;
    padding: 0 8px;
    border: 0;
    border-radius: 999px;
    background: var(--line);
    font-size: 11px;
    font-weight: 500;
    line-height: 1;
    white-space: nowrap;
  }
  .with-icon {
    padding-left: 6px;
  }
  /* The icon takes the chip's ink. Two classes outrank a container's own icon rule, such as the clip
     card's grey for the icons on its detail lines. */
  .badge.with-icon :global(svg) {
    width: 12px;
    height: 12px;
    color: inherit;
    stroke-width: 2.4;
  }
  .accent {
    background: var(--accent);
  }
  .success { background: var(--success); }
  .violet { background: var(--violet); }
  .sky { background: var(--sky); }
  .danger { background: var(--danger); }
  .warn {
    background: var(--warn);
  }
  @media (forced-colors: active) { .badge { border: 1px solid CanvasText; forced-color-adjust: auto; } }
</style>
