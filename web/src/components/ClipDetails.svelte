<script lang="ts">
  import type { ApiError, Schemas } from "../lib/api";
  import { clipDetails } from "../lib/clip-details";
  import { formatClock, formatFps } from "../lib/format";
  import type { ClipDetails, ClipSummary } from "../lib/library";
  import { metadataHelp } from "../lib/metadata";
  import { recordedMetadata } from "../lib/clip-metadata";
  import { bridge } from "../lib/bridge";
  import { displaySize } from "../lib/display-size";
  import ResolutionWarning from "./ResolutionWarning.svelte";
  import SendAction from "./SendAction.svelte";
  import SendIndicator from "./SendIndicator.svelte";
  import { sendChoices, type SendTarget } from "../lib/send";
  import GyroData from "./GyroData.svelte";
  import ClipAnalytics from "./ClipAnalytics.svelte";
  import ErrorNote from "./ErrorNote.svelte";
  import MetadataTree from "./MetadataTree.svelte";
  import CalibrationVisuals from "./CalibrationVisuals.svelte";
  import RawDiagnostics from "./RawDiagnostics.svelte";
  import ReportActions from "./ReportActions.svelte";
  import Readiness from "./Readiness.svelte";

  // `clip` is the library's summary; the details are read from the engine whenever it changes.
  // Both Resolve workflows take the selected RAW clips.
  let { clip, raw = 0, onsend, mounts = [], mountBusy = false, onmountaction, playing = false, section = "all", showActions = true, label = "Clip details", titled = true }: {
    section?: "all" | "info" | "analytics" | "gyro" | "metadata" | "media";
    showActions?: boolean;
    label?: string; titled?: boolean; // untitled: a section already headed with the clip, as in the inspector
    playing?: boolean;
    mounts?: Schemas["MountedSequence"][]; mountBusy?: boolean;
    onmountaction?: (record: Schemas["MountedSequence"], action: "unmount" | "remount") => void;
    clip: ClipSummary; raw?: number; onsend?: (target: SendTarget) => void;
  } = $props();
  let details = $state<ClipDetails | null>(null);
  let problem = $state<ApiError | null>(null);
  let request = 0;
  let previousId: string | null = null;
  let calibrationOpen = $state(false);

  $effect(() => {
    const id = clip.id;
    const asked = ++request;
    if (id !== previousId) { details = null; problem = null; }
    previousId = id;
    if (id === null) return;
    void clipDetails(id, clip).then((result) => {
      if (asked !== request) return; // another clip was chosen meanwhile
      if (result.ok) details = result.data;
      problem = result.ok ? null : result;
    });
  });



  async function locate(path: string): Promise<void> {
    const id = clip.id;
    try { await $bridge?.api.reveal_file?.(path); }
    catch {
      if (clip.id === id) problem = { ok: false, code: "open-file-location-failed", message: "Could not open the file location" };
    }
  }

  let shown = $derived(details !== null && details.id === clip.id ? details : null);
  let format = $derived(shown === null ? "" : [
    shown.width != null && shown.height != null ? `${shown.width} x ${shown.height}` : null,
    shown.fps != null ? formatFps(shown.fps) : null,
  ].filter((part) => part !== null).join(" · "));
  let display = $derived(displaySize(shown ?? clip));
  let metadata = $derived(shown ? recordedMetadata(shown) : null);
</script>

<aside aria-label={label}>
  {#if showActions}
  <div class="actions">
    <button disabled={clip.id === null} onclick={() => { if (clip.id) window.location.hash = `/develop/${encodeURIComponent(clip.id)}`; }}>Open Resolve Settings</button>
    {#if onsend && raw > 0}
      <SendAction onclick={onsend} count={raw} showCount={raw > 1} targets={sendChoices} />
    {/if}
  </div>
  {#if clip.id}<SendIndicator id={clip.id} />{/if}
  {/if}
  {#if (section === "all" || section === "info") && !titled}
  {#if format}<p class="format">{format} <ResolutionWarning width={display.width} height={display.height} raw={clip.kind === "raw"} /></p>{/if}
  {:else if section === "all" || section === "info"}
  <header class="metadata-header">
    <div><span class="eyebrow">CLIP INFORMATION</span><h2 title={clip.path}>{clip.name}</h2>{#if format}<p class="format">{format} <ResolutionWarning width={display.width} height={display.height} raw={clip.kind === "raw"} /></p>{/if}</div>
    <span class="kind">{clip.kind === "raw" ? "RAW" : "LOG"}</span>
  </header>
  {/if}
  {#if problem}<ErrorNote error={problem} />{/if}
  {#if section === "analytics" && shown?.id}{#key shown.id}<ClipAnalytics id={shown.id} name={shown.name} raw={shown.kind === "raw"} gyro={shown.gyro} />{/key}{/if}
  {#if (section === "all" || section === "gyro") && shown?.gyro && shown.id}{#key shown.id}<GyroData id={shown.id} expanded={section === "gyro"} />{/key}{/if}
  {#if (section === "all" || section === "info") && shown?.orientation_hint?.appears_wrong}
    <p class="orientation-note" role="status">{shown.orientation_hint.message} Use Resolve Settings to change Orientation.</p>
  {/if}
  {#if shown && metadata}
    {#if section === "all" || section === "info"}
    <dl class="statistics" aria-label="Recording summary">
      {#if shown.duration_s != null}<div><dt title={metadataHelp("Duration")}>Duration</dt><dd>{formatClock(shown.duration_s)}</dd></div>{/if}
      {#if shown.frame_count != null}<div><dt title={metadataHelp("Frames")}>Frames</dt><dd>{shown.frame_count}</dd></div>{/if}
      {#if shown.sensor_bits != null}<div><dt title={metadataHelp("Sensor depth")}>{shown.kind === "raw" ? "Sensor depth" : "Video depth"}</dt><dd>{shown.sensor_bits}-bit</dd></div>{/if}
    </dl>
    {#if shown.kind === "raw"}<p class="capture-note">Capture values are from the first recorded frame.</p>{/if}
    <div class="metadata-sections">
      {#each metadata.sections as section (section.name)}
        <section class="metadata-group" aria-label={section.name}>
          <h3>{section.name}</h3>
          <dl class="facts">{#each section.facts as fact, index (`${fact.name}:${index}`)}
            <div title={fact.help}><dt>{fact.name}</dt><dd class:path={fact.path}>{#if fact.path && $bridge?.api.reveal_file}<button class="source-path" title="Locate file" onclick={() => void locate(fact.value)}>{fact.value}</button>{:else}{fact.value}{/if}</dd></div>
          {/each}</dl>
        </section>
      {/each}
    </div>
    <h3>Readiness</h3>
    <Readiness clip={shown} />
    {/if}
    {#if section === "all" || section === "metadata"}
    {#if Object.keys(metadata.calibration).length > 0}
      <details class="foldout" ontoggle={(event) => calibrationOpen = event.currentTarget.open}><summary><strong>Calibration</strong><span>Colour, white balance and lens shading</span></summary>{#if calibrationOpen}<CalibrationVisuals values={metadata.calibration} />{/if}<MetadataTree values={metadata.calibration} /><ReportActions value={metadata.calibration} filename={`${shown.name}.calibration.json`} label="calibration" /></details>
    {/if}
    {#if shown.kind === "raw" && shown.id}{#key shown.id}<RawDiagnostics id={shown.id} name={shown.name} {playing} />{/key}{/if}
    {#if shown.metadata && Object.keys(shown.metadata).length > 0}
      <details class="foldout"><summary><strong>All recorded fields</strong><span>Complete source metadata</span></summary>
        {#each Object.entries(shown.metadata) as [name, values] (name)}
          {#if values && typeof values === "object" && !Array.isArray(values) && Object.keys(values).length > 0}
            <h4>{name}</h4><MetadataTree values={values as Record<string, unknown>} />
          {/if}
        {/each}
        <ReportActions value={shown.metadata} filename={`${shown.name}.metadata.json`} label="recorded metadata" />
      </details>
    {/if}
    {/if}
  {:else if !problem}
    <p class="muted">Reading the clip...</p>
  {/if}
  {#if (section === "all" || section === "media") && mounts.length > 0}
    <h3>Virtual media</h3>
    {#each mounts as mount (mount.id)}
      <div class="mount">
        <p>{mount.active ? !mount.source_available ? "Source offline or changed" : mount.serving ? `Mounted · ${mount.in_use === true ? "In use" : mount.in_use === false ? "Idle" : "Usage unavailable"}` : "Mount service stopped" : "Unmounted"}</p>
        <p class="path">{mount.path}</p>
        <p class="muted">{mount.sent ? "Sent media" : "Prepared media"} · {mount.raw_settings?.rotation ? `Display turn: ${mount.raw_settings.rotation}° clockwise` : "Recorded orientation"}</p>
        {#if mount.raw_settings?.level_policy === "recorded"}<p class="muted">Saved levels: recorded per frame</p>
        {:else if mount.raw_settings?.level_policy === "static"}<p class="muted">Saved levels: static (recording frame 1)</p>
        {:else}<p class="muted">Saved black: {Array.isArray(mount.raw_settings?.black_level) ? mount.raw_settings.black_level.join(", ") : mount.raw_settings?.black_level ?? "recorded per frame"} · white: {mount.raw_settings?.white_level ?? "recorded per frame"}</p>{/if}
        <p class="muted">Shading: {Math.round((mount.raw_settings?.lens_shading ?? 1) * 100)}% · {mount.raw_settings?.lens_shading_mode === "colour" ? "colour only" : mount.raw_settings?.lens_shading_mode === "vignette" ? "vignette only" : "vignette and colour"} · {mount.raw_settings?.mount_shading === "baked" ? "baked into the values" : "as gain maps"}{mount.raw_settings?.mount_proxy && mount.raw_settings.mount_proxy !== "full" ? ` · ${mount.raw_settings.mount_proxy} size proxy` : ""}{mount.raw_settings?.mount_frame_rate != null && mount.raw_settings.mount_frame_rate !== "keep" ? ` · ${mount.raw_settings.mount_frame_rate} fps` : ""}{mount.raw_settings?.mount_normalise_exposure === "picture" ? " · exposure normalised from the picture" : mount.raw_settings?.mount_normalise_exposure === "recorded" ? " · exposure normalised" : ""}{mount.raw_settings?.mount_transfer === "log" ? " · log encoded" : ""}{mount.raw_settings?.mount_crop === "auto" ? " · black edge removed" : mount.raw_settings?.mount_crop && mount.raw_settings.mount_crop !== "none" ? ` · crop ${mount.raw_settings.mount_crop}` : ""}{mount.raw_settings?.camera_model_source !== "recorded" && mount.raw_settings?.mount_camera_model ? ` · named ${mount.raw_settings.mount_camera_model}` : ""}</p>
        {#if mount.active}<button class="secondary" disabled={mountBusy || mount.in_use === true} title={mount.in_use ? "Media has open files in a receiving application" : "Unmount virtual media"} onclick={() => onmountaction?.(mount, "unmount")}>Unmount</button>{/if}
        <button class="secondary" disabled={mountBusy || !mount.source_available || mount.in_use === true} title={!mount.source_available ? "Source changed or is offline; send the clip again to prepare its current media" : mount.in_use ? "Close this media in the receiving application before reconnecting it" : "Reconnect this exact media path with its saved settings"} onclick={() => onmountaction?.(mount, "remount")}>Remount</button>
        {#if !mount.source_available}<p class="muted">Source changed or is offline. Send the clip again to prepare its current media.</p>{/if}
      </div>
    {/each}
    <p class="muted">Each path keeps its saved calibration. Remount restores those settings. Unmounting disconnects this media from your editor. Mount or send the clip again when you need it.</p>
  {/if}
  {#if section === "media" && mounts.length === 0}<p class="muted">No virtual media mounted. Use Mount on the clip to create a CinemaDNG folder without opening Resolve.</p>{/if}

</aside>

<style>
  aside {
    padding: 16px;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
  }
  .metadata-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
  .eyebrow { color: var(--accent); font-size: 10px; letter-spacing: 0.12em; }
  h2 { margin: 6px 0; overflow-wrap: anywhere; font-size: 15px; font-weight: 600; }
  .format { margin: 0; color: var(--muted); font-size: 12px; }
  .kind { padding: 4px 9px; border-radius: 999px; background: var(--accent); color: #000; font: 11px var(--mono); }
  .statistics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 18px 0 12px; }
  .statistics div { padding: 12px 10px; border: 1px solid var(--line); border-radius: 8px; }
  .statistics dt { color: var(--accent); font-size: 10px; }
  .statistics dd { margin: 5px 0 0; font-size: 17px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .capture-note { margin: 0 0 14px; font-size: 11px; color: var(--muted); }
  .metadata-sections { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr)); gap: 10px; }
  .metadata-group { padding: 13px; border: 1px solid var(--line); border-radius: 8px; min-width: 0; }
  h3 { margin: 18px 0 8px; font-size: 12px; font-weight: 600; color: var(--accent); }
  .metadata-group[aria-label="File"] { grid-column: 1 / -1; }
  .metadata-group h3 { margin: 0 0 10px; color: var(--accent); }
  h4 { margin: 16px 0 8px; font-size: 12px; color: var(--accent); }
  .facts { margin: 0; font-size: 12px; }
  .facts div { display: grid; grid-template-columns: minmax(90px, 38%) minmax(0, 1fr); gap: 10px; padding: 5px 0; }
  dt { color: var(--accent); }
  dd { margin: 0; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
  .foldout { margin-top: 10px; padding: 14px; border: 1px solid var(--line); border-radius: 8px; }
  summary { cursor: pointer; font-size: 12px; }
  summary strong { font-weight: 600; }
  summary span { display: block; margin: 5px 0 0 15px; color: var(--muted); font-size: 11px; }
  .foldout[open] summary { margin-bottom: 14px; }
  .path {
    font-family: var(--mono);
    user-select: text;
  }
  .mount { margin-top: 10px; font-size: 12px; }
  .orientation-note { color: var(--warn); padding: 10px; border: 1px solid var(--warn); border-radius: 6px; font-size: 12px; }
  .mount p { margin: 4px 0; }
  .muted {
    color: var(--muted);
    font-size: 12px;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 0 0 18px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--line);
  }
  .source-path { padding: 0; border: 0; background: transparent; color: var(--accent); font: inherit; text-align: left; text-decoration: underline; text-underline-offset: 3px; overflow-wrap: anywhere; cursor: pointer; }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  .secondary {
    background: transparent;
    border: 1px solid var(--line);
    color: var(--text);
  }
</style>
