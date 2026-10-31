<script lang="ts">
  import type { Schemas } from "../lib/api";
  import { formatClock, formatFrameSize, mountedRate } from "../lib/format";
  import { thumbnailUrl, thumbnailRevisions, type ClipSummary } from "../lib/library";
  import { displaySize } from "../lib/display-size";
  import ResolutionWarning from "./ResolutionWarning.svelte";
  import Icon from "./Icon.svelte";
  import ClipRating from "./ClipRating.svelte";
  import { annotationsFor } from "../lib/annotations";
  import SendIndicator from "./SendIndicator.svelte";
  import Badge from "./Badge.svelte";
  import { warmPreview, hoverPreview, leavePreview } from "../lib/preview-opening";
  import { skims, skimKey, wantSkim } from "../lib/skims";

  let { clip, selected, active = false, menuOpen = false, onchoose, oncontext, position = 1, prefetch = true, mounts = [], mounting = false }: {
    mounting?: boolean;
    mounts?: Schemas["MountedSequence"][];
    menuOpen?: boolean; active?: boolean; clip: ClipSummary; selected: boolean; onchoose: (event: MouseEvent | KeyboardEvent) => void;
    position?: number; prefetch?: boolean;
    oncontext?: (event: MouseEvent | KeyboardEvent) => void;
  } = $props();
  const BADGES = {
    recovered: { label: "Recovered", tone: "warn" },
    "dropped-frames": { label: "Dropped frames", tone: "warn" },
  } as const;
  let activeMounts = $derived(mounts.filter(mount => mount.active));
  let inUse = $derived(activeMounts.some(mount => mount.in_use === true));
  let usageLabel = $derived(inUse ? "In use" : activeMounts.some(mount => mount.in_use == null) ? "Usage unavailable" : "Idle");
  let mountProblem = $derived(activeMounts.some(mount => !mount.serving || !mount.source_available));
  let failed = $state<string | null>(null);
  let thumbnail = $derived(clip.id ? `${thumbnailUrl(clip.id)}${$thumbnailRevisions[clip.id] ? `?revision=${$thumbnailRevisions[clip.id]}` : ""}` : "");
  // Skimming: the pointer across the thumbnail shows that point of the clip, once the pointer has rested on
  // the card long enough to ask the engine for the clip's pictures (one clip at a time, lib/skims).
  let revision = $derived(clip.id ? $thumbnailRevisions[clip.id] ?? 0 : 0);
  let skimAt = $state<number | null>(null); // from 0 (start) to 1 (end), while the pointer is over the thumbnail
  let frames = $derived(clip.id ? $skims[skimKey(clip.id, revision)] : undefined);
  let skimmed = $derived(frames && skimAt !== null ? frames[Math.min(frames.length - 1, Math.floor(skimAt * frames.length))] : undefined);
  let resting: ReturnType<typeof setTimeout> | undefined;
  function skim(event: PointerEvent): void {
    if (!openable || !clip.id || event.pointerType === "touch") return;
    const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
    skimAt = Math.min(1, Math.max(0, (event.clientX - box.left) / Math.max(box.width, 1)));
    if (frames || resting !== undefined) return;
    const id = clip.id, wanted = revision;
    resting = setTimeout(() => { resting = undefined; wantSkim(id, wanted); }, 250);
  }
  function stopSkimming(): void {
    skimAt = null;
    clearTimeout(resting);
    resting = undefined;
  }
  let openable = $derived(clip.status === "ready" && clip.id !== null);
  let display = $derived(displaySize(clip));
  let format = $derived([
    clip.width != null && clip.height != null ? formatFrameSize(clip.width, clip.height) : null,
  ].filter((part) => part !== null).join(" · "));
  let timing = $derived([
    clip.measured_fps != null || clip.fps != null ? { label: "FPS:", value: mountedRate(clip.measured_fps ?? clip.fps ?? 0) } : null,
    clip.frame_count != null ? { label: "Frames:", value: clip.frame_count.toLocaleString() } : null,
    clip.kind === "raw" ? { label: "Dropped:", value: clip.missing_frames ?? "unknown" } : null,
    clip.kind === "raw" ? { label: "Duplicated:", value: clip.duplicated_frames ?? "unknown" } : null,
  ].filter(part => part !== null));
  function key(event: KeyboardEvent): void {
    if (/^[0-5]$/.test(event.key) && !event.ctrlKey && !event.metaKey && !event.altKey && openable && clip.id) {
      event.preventDefault(); event.stopPropagation(); void annotationsFor(clip.id).rate(Number(event.key)); return;
    }
    if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
      event.preventDefault(); oncontext?.(event); return;
    }
    if (event.key === " " && selected) return; // The selected preview transport owns Space.
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    if (openable) onchoose(event);
  }
</script>

<div role="option" tabindex={openable ? 0 : -1} aria-label={clip.name} aria-selected={selected}
  aria-current={active ? "true" : undefined} data-active={active ? "true" : undefined} aria-disabled={!openable} class:active class:selected class:copying={clip.status === "copying"}
  oncontextmenu={(event) => { if (openable) { event.preventDefault(); oncontext?.(event); } }}
  title={clip.path} onclick={(event) => { if (openable) onchoose(event); }} onkeydown={key}
  onpointerenter={() => { if (openable && clip.id && prefetch && !selected) hoverPreview(clip.id); }}
  onpointerleave={() => { if (clip.id) leavePreview(clip.id); }}
  onfocus={() => { if (openable && clip.id && prefetch && !selected) warmPreview(clip.id); }}>
  <div class="thumbnail-slot">
    <div class="thumbnail" role="presentation" onpointermove={skim} onpointerleave={stopSkimming}>
    <input class="pick clip-selection" type="checkbox" aria-label={`Select ${clip.name}`} title={selected ? "Deselect clip" : "Select clip"} checked={selected} disabled={!openable}
      onclick={(event) => { event.stopPropagation(); if (openable) onchoose(new MouseEvent("click", { ctrlKey: true, shiftKey: event.shiftKey })); }} onkeydown={(event) => event.stopPropagation()} />
      <span class="number">{String(position).padStart(2, "0")}</span>
      {#if openable && clip.id !== null && failed !== thumbnail}
        <!-- A blurred copy fills the space the whole frame leaves, however the card is shaped. -->
        <img class="backdrop" src={thumbnail} alt="" aria-hidden="true" loading="lazy" onerror={() => failed = thumbnail} />
        <img class="frame" src={skimmed ?? thumbnail} alt="" loading="lazy" onerror={() => failed = thumbnail} />
        {#if skimAt !== null}<span class="skim" aria-hidden="true" style:left={`${skimAt * 100}%`}></span>{/if}
      {:else}<span class="fallback">{clip.name}</span>{/if}
      {#if clip.duration_s != null}<span class="duration">{formatClock(skimAt !== null ? skimAt * clip.duration_s : clip.duration_s)}</span>{/if}
    </div>
  </div>
  <div class="body">
    <div class="heading"><h3>{clip.name}</h3>{#if clip.badges?.includes("has-settings")}<span class="scope" title="This clip has saved settings overrides">Clip settings</span>{:else}<span class="scope" title="Uses inherited application and folder settings">Defaults</span>{/if}</div>
    <!-- The send status and rating share the camera's line, leaving the thumbnail the card's whole height. -->
    <div class="meta">
      {#if clip.camera}<p class="camera">{clip.camera}</p>{/if}
      {#if clip.id && clip.status !== "copying" && clip.status !== "unreadable"}
        <SendIndicator id={clip.id} />
        <ClipRating id={clip.id} name={clip.name} rating={clip.rating ?? 0} />
      {/if}
    </div>
    {#if clip.status === "copying"}
      <p class="muted">Copying...</p>
    {:else if clip.status === "unreadable"}
      <p class="error">{clip.error ?? "This clip cannot be read"}</p>
    {:else}
      <p class="format"><Icon name="film" /><span class="kind">{clip.kind === "raw" ? "RAW" : "LOG"}</span>{#if format}<span class="format-pill">{format}</span><ResolutionWarning width={display.width} height={display.height} raw={clip.kind === "raw"} />{/if}</p>
      {#if clip.kind === "raw"}
        <p class="capture"><Icon name="camera" /><span>{[
          clip.focal_length_mm != null ? `${clip.focal_length_mm.toFixed(1)} mm (35 mm eq.)` : null,
          clip.aperture != null ? `f/${clip.aperture.toFixed(1)}` : null,
          clip.iso != null ? `ISO ${Math.round(clip.iso)}` : null,
          clip.exposure_time_ns != null && clip.exposure_time_ns > 0 ? `1/${Math.round(1e9 / clip.exposure_time_ns)}s` : null,
        ].filter(Boolean).join(" · ")}</span></p>
      {/if}
      {#if clip.measured_fps != null || clip.fps != null || clip.frame_count != null}
        <p class="technical"><Icon name="clock" /><span class="timing">
          {#each timing as part, index}<span>{#if index > 0}<span class="separator" aria-hidden="true">{" | "}</span>{/if}<strong>{part.label}</strong> {part.value}</span>{/each}
        </span></p>
      {/if}
      <div class="badges">
        {#if clip.gyro}<span title="Gyro log or embedded telemetry found"><Badge label="Gyro" tone="violet" icon="gyro" /></span>{/if}
        {#if clip.gps}<span title="Recorded GPS location available"><Badge label="GPS" tone="sky" icon="location" /></span>{/if}
        {#if clip.orientation_hint?.appears_wrong}<span title={clip.orientation_hint.message}><Badge label="Check orientation" tone="warn" /></span>{/if}
        {#each (clip.badges ?? []).filter(badge => badge !== "has-settings") as badge (badge)}<Badge label={BADGES[badge].label} tone={BADGES[badge].tone} />{/each}
        {#if mounting || activeMounts.length > 0 || (openable && clip.kind === "raw")}
          <span class="mount">
            {#if mounting}
              <span role="status"><Badge label="Mounting" tone="warn" /></span>
            {:else if activeMounts.length > 0}
              <Badge label={mountProblem ? "Mount needs attention" : "Mounted"} tone={mountProblem ? "warn" : "success"} />
              {#if mounts.some(mount => mount.sent)}<span title="Tracks open files and folders. Idle does not mean this media is absent from a saved editor project."><Badge label={usageLabel} tone={inUse ? "warn" : "accent"} /></span>{/if}
            {:else}
              <Badge label="Unmounted" tone="danger" />
            {/if}
          </span>
        {/if}
      </div>
    {/if}
  </div>
  <button class="more" aria-label={`More actions for ${clip.name}`} aria-haspopup="menu" aria-expanded={menuOpen} disabled={!openable || !oncontext}
    title="Clip actions (also available with right-click)" onclick={(event) => { event.stopPropagation(); oncontext?.(event); }} onkeydown={(event) => event.stopPropagation()}><Icon name="more" /></button>
</div>

<style>
  /* Cards out of view skip layout and paint: with a few hundred clips, choosing one or clearing a search
     otherwise lays out every card again (0.4 s at 200 clips). */
  [role="option"] { display: grid; position: relative; --thumbnail: clamp(150px, 30%, 300px); grid-template-columns: var(--thumbnail) minmax(0, 1fr); gap: 16px; padding: 15px 16px; align-items: center; background: var(--panel); border: 1px solid color-mix(in srgb, var(--muted) 30%, var(--panel)); border-radius: 9px; cursor: pointer; user-select: none; transition: transform .15s ease, box-shadow .15s ease, background-color .15s ease; content-visibility: auto; contain-intrinsic-size: auto 180px; }
  @media (hover: hover) and (pointer: fine) { [role="option"]:hover { transform: translateY(-2px) scale(1.006); box-shadow: 0 0 15px color-mix(in srgb, var(--accent) 18%, transparent), 0 4px 12px #0003; z-index: 1; } }
  [role="option"]:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .selected { background: color-mix(in srgb, var(--accent) 13%, var(--panel)); }
  .active .heading h3 { color: var(--accent); }
  [aria-disabled="true"] { cursor: default; } .copying { opacity: .5; }
  /* The thumbnail takes the card's left side to its edges: the margins undo the card's padding. */
  .thumbnail-slot { align-self: stretch; display: grid; min-width: 0; margin: -15px 0 -15px -16px; }
  .thumbnail { position: relative; min-height: 100%; background: #080d10; border-radius: 8px 0 0 8px; overflow: hidden; }
  .pick { position: absolute; z-index: 2; top: 8px; left: 8px; margin: 0; cursor: pointer; box-shadow: 0 1px 5px #0008; }
  img { display: block; width: 100%; height: 100%; }
  .frame { position: relative; object-fit: contain; }
  .backdrop { position: absolute; inset: 0; object-fit: cover; filter: blur(16px) brightness(.5); transform: scale(1.15); }
  .skim { position: absolute; z-index: 1; top: 0; bottom: 0; width: 2px; margin-left: -1px; background: var(--accent); pointer-events: none; }
  .fallback { display: grid; place-items: center; height: 100%; padding: 8px; color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
  .duration { position: absolute; right: 7px; bottom: 7px; padding: 3px 7px; border: 1px solid #ffffff20; border-radius: 6px; background: #080d10da; color: #e5eeef; font-size: 11px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .number { position: absolute; top: 8px; right: 7px; z-index: 1; padding: 1px 4px; border-radius: 3px; background: #080d10cc; color: #e5eeef; font: 10px monospace; }
  .body { min-width: 0; padding-bottom: 3px; }
  .heading { display: flex; gap: 7px; align-items: center; } h3 { min-width: 0; margin: 0; overflow: hidden; font-size: 13px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
  .meta { display: flex; align-items: center; flex-wrap: wrap; gap: 2px 12px; margin: 5px 0 8px; }
  .camera { margin: 0; color: var(--muted); font-size: 11px; }
  .meta :global(.rating) { margin: 0 0 0 auto; }
  .meta :global(.rating button) { min-width: 20px; padding: 1px 2px; font-size: 15px; }
  .meta :global(.send-state) { margin-top: 0; font-size: 10px; }
  .scope { white-space: nowrap; margin-left: auto; padding: 2px 7px; background: var(--line); border-radius: 999px; font-size: 9px; color: #fff; }
  p { margin: 5px 0 0; font-size: 11px; line-height: 1.45; } .muted { color: var(--muted); } .error { color: var(--fail); overflow-wrap: anywhere; }
  .format, .capture, .technical { display: flex; align-items: center; gap: 8px; }
  .body :global(svg) { width: 14px; height: 14px; flex-shrink: 0; color: var(--muted); }
  .kind, .format-pill { border-radius: 999px; padding: 1px 7px; font-size: 10px; font-weight: 600; }
  .kind { color: #000; background: var(--accent); } .format-pill { background: var(--line); color: #fff; }
  .technical, .capture { color: var(--muted); font-variant-numeric: tabular-nums; } .technical strong { color: var(--text); font-weight: 450; }
  .timing { display: flex; flex-wrap: wrap; gap: 2px 0; }
  .timing > span { white-space: nowrap; }
  .separator { margin: 0 5px; }
  .badges { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; margin-top: 10px; padding-right: 20px; }
  .badges > span { display: inline-flex; }
  .mount { gap: 5px; margin-left: auto; } /* the mount's state, at the row's end */
  .more { position: absolute; right: 9px; bottom: 10px; display: grid; place-items: center; padding: 5px; border: 1px solid transparent; border-radius: 6px; background: transparent; color: var(--muted); }
  .more:hover, .more:focus-visible { color: var(--accent); background: var(--panel); border-color: var(--line); } .more:disabled { opacity: .4; }
  .more :global(svg) { width: 18px; height: 18px; }
  @media (max-width: 650px) { [role="option"] { --thumbnail: 104px; gap: 11px; padding: 12px; } .thumbnail-slot { margin: -12px 0 -12px -12px; } .scope { display: none; } .capture { font-size: 10px; } }
  @media (prefers-reduced-motion: reduce) { [role="option"] { transition: none; } [role="option"]:hover { transform: none; } }
  @media (forced-colors: active) { .selected { outline: 2px solid Highlight; outline-offset: -2px; } }
</style>
