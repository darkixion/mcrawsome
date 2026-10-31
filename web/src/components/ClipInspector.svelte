<script lang="ts">
  import type { Snippet } from "svelte";
  import type { Schemas } from "../lib/api";
  import { formatClock, formatFps } from "../lib/format";
  import type { ClipSummary } from "../lib/library";
  import ClipDetails from "./ClipDetails.svelte";
  import ClipNotes from "./ClipNotes.svelte";
  import SectionCard from "./SectionCard.svelte";

  let { clip, mounts, mountBusy, mountAvailable = true, mountReason = "", playing, develop, savingText,
    tab = $bindable("develop"), onmount, onmountaction, onnext }: {
    clip: ClipSummary;
    mounts: Schemas["MountedSequence"][]; mountBusy: boolean; mountAvailable?: boolean; mountReason?: string; playing: boolean;
    develop: Snippet; savingText: string; tab?: string;
    onmount: () => void;
    onmountaction: (record: Schemas["MountedSequence"], action: "unmount" | "remount") => void;
    onnext?: () => void;
  } = $props();
  const identity = $props.id();
  // Info, Analytics and Metadata share Clip info as folding sections; a remembered Analytics or Metadata tab opens it.
  const tabs = [
    { id: "develop", label: "Resolve Settings" }, { id: "info", label: "Clip info" },
    { id: "notes", label: "Notes" }, { id: "media", label: "Media" },
  ];
  let active = $derived(tabs.some(item => item.id === tab) ? tab : "info");
  let infoSummary = $derived([
    clip.width != null && clip.height != null ? `${clip.width} x ${clip.height}` : null,
    clip.fps != null ? formatFps(clip.fps) : null,
    clip.duration_s != null ? formatClock(clip.duration_s) : null,
  ].filter(part => part !== null).join(" · "));
  function keyboard(event: KeyboardEvent): void {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const index = tabs.findIndex(item => item.id === active);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
      : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tab = tabs[next]!.id;
    document.getElementById(`${identity}-${tab}`)?.focus();
  }
</script>

<aside class="inspector" aria-label="Clip workspace">
  <div class="bar">
    <div role="tablist" tabindex="-1" aria-label="Clip information and adjustments" onkeydown={keyboard}>
      {#each tabs as item (item.id)}
        <button role="tab" id={`${identity}-${item.id}`} aria-controls={`${identity}-panel`} aria-selected={active === item.id} tabindex={active === item.id ? 0 : -1} onclick={() => tab = item.id}>{item.label}</button>
      {/each}
    </div>
    <p class="caption"><span class="name" title={clip.path}>{clip.name}</span><span role="status" aria-label="Resolve settings save status">{savingText}</span></p>
  </div>
  <div class="panel" id={`${identity}-panel`} role="tabpanel" aria-labelledby={`${identity}-${active}`} tabindex="0">
    <div hidden={active !== "develop"}>{@render develop()}</div>
    {#if active === "info"}
      <div class="cards">
        <SectionCard id="clip-info" title="Info" summary={infoSummary} initiallyOpen>
          <ClipDetails {clip} {playing} {mounts} {mountBusy} {onmountaction} showActions={false} titled={false} section="info" />
        </SectionCard>
        <SectionCard id="clip-analytics" title="Analytics" summary="Frame timing and capture checks">
          <ClipDetails {clip} {playing} showActions={false} label="Clip analytics" section="analytics" />
        </SectionCard>
        <SectionCard id="clip-metadata" title="Metadata" summary="Calibration, RAW diagnostics and recorded fields">
          <ClipDetails {clip} {playing} showActions={false} label="Clip metadata" section="metadata" />
        </SectionCard>
      </div>
    {/if}
    {#if active === "notes" && clip.id}<ClipNotes focusOnLoad id={clip.id} name={clip.name} {onnext} place={clip.place ?? null} placeKm={clip.place_km ?? null} />{/if}
    {#if active === "media"}
      {#if clip.kind === "raw" && !mounts.some(record => record.active)}<button disabled={mountBusy || !mountAvailable} title={mountAvailable ? "Mount virtual CinemaDNG without opening Resolve" : mountReason} onclick={onmount}>Mount with current settings</button>{/if}
      <ClipDetails {clip} {playing} {mounts} {mountBusy} {onmountaction} showActions={false} section="media" />
    {/if}
  </div>
</aside>

<style>
  .inspector { container-type: inline-size; min-width: 0; position: sticky; top: 0; max-height: calc(100dvh - 190px); overflow: auto; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); }
  /* One line of tabs, underlined when chosen, then the clip's name and whether its settings are saved. */
  .bar { display: flex; align-items: center; gap: 12px; padding: 0 14px; background: var(--panel); border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 3; }
  [role="tablist"] { display: flex; flex: none; gap: 18px; }
  .caption { display: flex; flex: 1; justify-content: flex-end; gap: 8px; min-width: 0; margin: 0; font-size: 11px; color: var(--muted); white-space: nowrap; }
  .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; color: var(--text); }
  @container (max-width: 400px) { .name { display: none; } }
  [role="tab"] { flex: none; margin-bottom: -1px; padding: 11px 0 9px; border: 0; border-bottom: 2px solid transparent; border-radius: 0; background: transparent; color: var(--muted); font-size: 12.5px; font-weight: 500; white-space: nowrap; }
  [role="tab"]:hover { color: var(--text); }
  [role="tab"][aria-selected="true"] { color: var(--text); font-weight: 600; border-bottom-color: var(--accent); }
  .panel { padding: 10px; }
  .cards { display: grid; gap: 8px; }
  .panel :global([hidden]) { display: none !important; }
  .panel :global(aside), .panel :global(.notes) { padding: 0; border: 0; margin: 0; background: transparent; }
  .panel > :global(aside), .panel > :global(.notes) { padding: 8px; }
  .panel :global(.selected) { margin: 0 0 12px; }
  .panel :global(summary) { color: var(--text); }
  .panel :global(input), .panel :global(select), .panel :global(textarea), .panel :global(output) { color: var(--text); }
  @media (max-width: 1000px) { .inspector { position: static; max-height: none; } }
</style>
