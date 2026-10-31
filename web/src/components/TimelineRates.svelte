<script lang="ts">
  import { onMount } from "svelte";
  import type { Schemas } from "../lib/api";
  // Shown before a timeline send mounts anything: the clips at rates a Resolve timeline can't take, with an
  // offer to mount them at their nearest whole rates.
  let { clips, onclose, onfix }: {
    clips: Schemas["TimelineRate"][]; onclose: () => void;
    onfix: (ids: string[]) => Promise<string>; // changes their Mounted frame rate and sends; a problem, or ""
  } = $props();
  let odd = $derived(clips.filter(clip => !clip.usable));
  let stuck = $derived(odd.filter(clip => clip.nearest === null));
  let wholes = $derived([...new Set(odd.map(clip => clip.nearest))]);
  let busy = $state(false);
  let message = $state("");
  let panel: HTMLElement;
  const listed = (items: Schemas["TimelineRate"][]) => items.map(clip => clip.name).join(", ");
  async function fix(): Promise<void> {
    busy = true; message = "";
    try { message = await onfix(odd.map(clip => clip.id)); }
    finally { busy = false; }
  }
  onMount(() => panel.querySelector<HTMLButtonElement>("button.fix")?.focus());
</script>
<section bind:this={panel} aria-label="Frame rates for the timeline" class="rates">
  <header><strong>Frame rates for the timeline</strong><button class="quiet" disabled={busy} onclick={onclose}>Cancel</button></header>
  <p>Resolve timelines need a whole number of frames per second, from 1 to 60. {odd.length === 1 ? "This clip mounts" : "These clips mount"} at another rate:</p>
  <ul>{#each odd as clip (clip.id)}<li><strong>{clip.name}</strong> {clip.rate} fps{#if clip.nearest !== null}, nearest whole rate {clip.nearest} fps{/if}</li>{/each}</ul>
  {#if stuck.length}
    <p>{listed(stuck)} can't go on a timeline at {stuck.length === 1 ? "its" : "their"} nearest whole rate. Send {stuck.length === 1 ? "it" : "them"} to the Media Bin, or set a fixed Mounted frame rate of 60 or less under RAW media in Resolve Settings.</p>
  {:else}
    <p>Mount {odd.length === 1 ? `it at ${odd[0]!.nearest} fps` : "them at their nearest whole rates"} instead? This sets Mounted frame rate to Nearest whole rate in {odd.length === 1 ? "its" : "their"} Resolve Settings, which Media Bin sends use too. Frames are repeated or dropped to keep the recorded timing, and the sound is unchanged.</p>
    <button class="fix" disabled={busy} onclick={() => void fix()}>{busy ? "Changing frame rates..." : wholes.length === 1 ? `Use ${wholes[0]} fps and send` : "Use whole rates and send"}</button>
  {/if}
  {#if message}<p role="alert">{message}</p>{/if}
</section>
<style>
  .rates { margin: 12px 0; padding: 12px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); font-size: 12px; }
  header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  p, li { overflow-wrap: anywhere; } ul { margin: 6px 0; padding-left: 18px; max-height: 160px; overflow-y: auto; }
  [role="alert"] { color: var(--error, #e8a2a2); }
</style>
