<script lang="ts">
  import { onMount } from "svelte";
  import { bridge } from "../lib/bridge";
  import ResetControls from "../components/ResetControls.svelte";
  import CacheManagement from "../components/CacheManagement.svelte";
  import ChoiceGroup from "../components/ChoiceGroup.svelte";
  import ErrorNote from "../components/ErrorNote.svelte";
  import Icon from "../components/Icon.svelte";
  import InfoTip from "../components/InfoTip.svelte";
  import CameraProfiles from "../components/CameraProfiles.svelte";
  import GuidedCameraProfiles from "../components/GuidedCameraProfiles.svelte";
  import FileAssociation from "../components/FileAssociation.svelte";
  import FileManagerMenu from "../components/FileManagerMenu.svelte";
  import { api, type ApiError, type Schemas } from "../lib/api";
  import { settings as shared, type InterfaceSettings } from "../lib/settings";

  let current = $state<InterfaceSettings | null>(null);
  let problem = $state<ApiError | null>(null);
  let saving = $state(false);
  let newFolder = $state("");
  let mountFolder = $state("");
  type Flag = "keep_engine" | "remount_on_start" | "preview_gpu" | "quad_bayer_remosaic" | "show_preview_stats" | "resolve_keywords";

  function show(saved: InterfaceSettings): void {
    current = saved;
    mountFolder = saved.mount_folder ?? "";
    shared.set(saved);
  }

  function fieldProblem(field: string): ApiError | null {
    return problem?.field === field ? problem : null;
  }

  type Changes = Partial<Schemas["SettingsUpdate"]> | ((settings: InterfaceSettings) => Partial<Schemas["SettingsUpdate"]>);
  async function performSave(changes: Changes): Promise<boolean> {
    if (current === null) return false;
    const { problem: _, mount_folder_default: __, ...values } = current;
    saving = true;
    const result = await api.saveSettings({ ...values, ...(typeof changes === "function" ? changes(current) : changes) });
    saving = false;
    if (!result.ok) { problem = result; return false; }
    problem = null;
    show(result.data);
    return true;
  }

  let saveQueue: Promise<void> = Promise.resolve();
  function save(changes: Changes): Promise<boolean> {
    const operation = saveQueue.then(() => performSave(changes));
    saveQueue = operation.then(() => undefined);
    return operation;
  }

  function changeCache(input: HTMLInputElement): void {
    if (input.value === "" || !input.validity.valid) {
      problem = { ok: false, code: "recipe-invalid", message: "Enter a whole number from 0 to 2048 MiB.", field: "preview_cache_mib" };
      return;
    }
    void save({ preview_cache_mib: input.valueAsNumber });
  }

  async function changeFlag(input: HTMLInputElement, field: Flag): Promise<void> {
    if (!await save({ [field]: input.checked })) input.checked = current?.[field] ?? (field === "preview_gpu" || field === "quad_bayer_remosaic");
  }

  type Edition = "free" | "studio";
  const editions: { value: Edition; label: string }[] = [{ value: "free", label: "Free" }, { value: "studio", label: "Studio" }];
  type Audio = "combined" | "linked" | "unlinked";
  const audioChoices: { value: Audio; label: string; description: string }[] = [
    { value: "combined", label: "With the video", description: "One Media Pool item holding video and audio." },
    { value: "linked", label: "Separate, kept linked", description: "Video and WAV items that stay together on a timeline." },
    { value: "unlinked", label: "Separate, unlinked", description: "Independent video and WAV items." },
  ];
  type Timecode = "zero" | "time-of-day";
  const timecodes: { value: Timecode; label: string }[] = [{ value: "zero", label: "From zero" }, { value: "time-of-day", label: "Time of day" }];
  type Zone = "computer" | "location";
  const zones: { value: Zone; label: string; description: string }[] = [
    { value: "computer", label: "This computer's time zone", description: "Every clip uses the time zone this computer is set to." },
    { value: "location", label: "Where it was recorded", description: "From the clip's GPS fix and the nearest city, offline; clips without a fix use this computer's." },
  ];
  let audio = $derived<Audio>(!current?.resolve_separate_audio ? "combined" : (current.resolve_link_audio ?? true) ? "linked" : "unlinked");
  let audioProblem = $derived(fieldProblem("resolve_separate_audio") ?? fieldProblem("resolve_link_audio"));

  // Combining audio keeps the saved link choice, so returning to separate items restores it.
  function changeAudio(choice: Audio): Promise<boolean> {
    return save(choice === "combined" ? { resolve_separate_audio: false } : { resolve_separate_audio: true, resolve_link_audio: choice === "linked" });
  }

  async function addFolder(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const folder = newFolder.trim();
    if (folder === "" || current === null) return;
    const same = (path: string): string => path.replace(/(?<=.)\/+$/, "");
    if ((current.folders ?? []).some(saved => same(saved) === same(folder))) {
      problem = { ok: false, code: "recipe-invalid", message: `${folder} is already in the list`, field: "folders" };
      return;
    }
    if (await save(latest => ({ folders: [...new Set([...(latest.folders ?? []), folder])] }))) newFolder = "";
  }

  async function chooseFolder(target: "library" | "mount" = "library"): Promise<void> {
    try {
      const selected = await $bridge?.api.choose_folder();
      if (!selected) return;
      if (target === "library") newFolder = selected;
      else await save({ mount_folder: selected });
    } catch {
      problem = { ok: false, code: "choose-folder-failed", message: "Could not open the folder dialog", field: target === "library" ? "folders" : "mount_folder" };
    }
  }

  function removeFolder(folder: string): void {
    void save(latest => ({ folders: (latest.folders ?? []).filter(kept => kept !== folder) }));
  }

  onMount(async () => {
    const loaded = await api.settings();
    if (loaded.ok) show(loaded.data);
    else problem = loaded;
  });
</script>

<section class="screen">
  <header class="page-heading">
    <div><h1>Settings</h1><p>Library, DaVinci Resolve, system and preview preferences.</p></div>
    <span class:failed={problem !== null} class="save-state" aria-live="polite">{saving ? "Saving…" : problem ? "Not saved" : "Changes save automatically"}</span>
  </header>
  {#if problem !== null && (problem.field === undefined || current === null)}<ErrorNote error={problem} />{/if}
  {#if current?.problem}
    <div class="card unreadable">
      <p>The saved settings could not be read, so these are the defaults. The file stays as it is until you change a setting here.</p>
      <ErrorNote error={{ code: "input-unreadable", message: current.problem }} />
    </div>
  {/if}

  {#if current !== null}
    <section class="card" aria-labelledby="general-settings">
      <header class="section-heading"><span class="section-icon"><Icon name="folder" /></span><h2 id="general-settings">General</h2><span>Library, mounts and startup</span></header>
      <div class="columns">
        <fieldset class="block" aria-label="Folders">
          <legend>Library folders <InfoTip label="Library folders" text="The Library searches these folders for MotionCam recordings. Removing a folder here does not delete its recordings." /></legend>
          <ul class="folders">
            {#each current.folders ?? [] as folder (folder)}
              <li><Icon name="folder" /><span class="path" title={folder}>{folder}</span><button type="button" class="quiet" aria-label="Remove {folder}" onclick={() => removeFolder(folder)}>Remove</button></li>
            {:else}<li class="empty">No folders yet.</li>{/each}
          </ul>
          <form class="row add-folder" onsubmit={addFolder}>
            <input type="text" aria-label="Add a folder" placeholder="/path/to/clips" bind:value={newFolder} spellcheck="false" />
            {#if $bridge?.api.choose_folder}<button type="button" class="secondary" aria-label="Choose folder" onclick={() => void chooseFolder()}>Browse…</button>{/if}
            <button type="submit">Add</button>
          </form>
          {#if fieldProblem("folders")}<ErrorNote error={fieldProblem("folders")!} />{/if}
          <div class="mount-folder">
          <div class="field-title"><label for="mount-folder">Mount folder</label><InfoTip label="Mount folder" text={`Defaults to ${current?.mount_folder_default ?? "~/Mounts/mcrawsome"}. New mounts use this folder; existing mounts keep their paths. No root access is needed.`} /></div>
          <div class="row"><input id="mount-folder" type="text" placeholder={current?.mount_folder_default ?? "~/Mounts/mcrawsome"} bind:value={mountFolder} spellcheck="false" onchange={() => void save({ mount_folder: mountFolder.trim() || null })} />
            {#if $bridge?.api.choose_folder}<button type="button" class="secondary" aria-label="Choose mount folder" onclick={() => void chooseFolder("mount")}>Browse…</button>{/if}
          </div>
          {#if fieldProblem("mount_folder")}<ErrorNote error={fieldProblem("mount_folder")!} />{/if}
          </div>
        </fieldset>

        <div class="block">
          <h3>Startup &amp; background</h3>
          <div class="option"><label class="check"><input type="checkbox" role="switch" checked={current.keep_engine} onchange={(event) => void changeFlag(event.currentTarget, "keep_engine")} />Keep the engine running in the background</label><InfoTip label="Keep the engine running" text="Keeps an idle engine running after the last window closes. Active exports and mounted media already keep the engine available while they need it." /></div>
          {#if fieldProblem("keep_engine")}<ErrorNote error={fieldProblem("keep_engine")!} />{/if}
          <div class="option"><label class="check"><input type="checkbox" role="switch" checked={current.remount_on_start} onchange={(event) => void changeFlag(event.currentTarget, "remount_on_start")} />Mount sent media again on start-up</label><InfoTip label="Mount sent media again" text="After the computer restarts, the engine mounts the media you sent to Resolve again at the same paths, so your projects find it online. Recordings that have changed or are offline are left unmounted." /></div>
          {#if fieldProblem("remount_on_start")}<ErrorNote error={fieldProblem("remount_on_start")!} />{/if}
        </div>
      </div>
    </section>

    <GuidedCameraProfiles />

    <section class="card" aria-labelledby="camera-profiles-settings">
      <header class="section-heading"><span class="section-icon"><Icon name="film" /></span><h2 id="camera-profiles-settings">Saved camera profiles</h2><span>Measured phone and lens calibration</span></header>
      <CameraProfiles />
    </section>

    <section class="card" aria-labelledby="resolve-settings">
      <header class="section-heading"><span class="section-icon"><Icon name="film" /></span><h2 id="resolve-settings">DaVinci Resolve</h2><span>How clips arrive in Resolve</span></header>
      <div class="resolve-choice">
        <span class="field-title">Edition</span>
        <div>
          <ChoiceGroup label="Edition" layout="segmented" choices={editions} value={current.resolve_full_resolution ? "studio" : "free"} onchange={(edition) => save({ resolve_full_resolution: edition === "studio" })} />
          <p class="choice-help">{current.resolve_full_resolution ? "Timelines keep the source resolution." : "Timelines larger than 3840 x 2160 are scaled down to fit. Mounted media stays at full resolution."}</p>
          {#if fieldProblem("resolve_full_resolution")}<ErrorNote error={fieldProblem("resolve_full_resolution")!} />{/if}
        </div>
      </div>
      <div class="resolve-choice">
        <span class="field-title">Audio</span>
        <div>
          <ChoiceGroup label="Audio" choices={audioChoices} value={audio} onchange={changeAudio} />
          {#if audioProblem}<ErrorNote error={audioProblem} />{/if}
        </div>
      </div>
      <div class="resolve-choice">
        <span class="field-title">Timecode</span>
        <div>
          <ChoiceGroup label="Timecode" layout="segmented" choices={timecodes} value={current.timecode ?? "zero"} onchange={(timecode) => save({ timecode })} />
          <p class="choice-help">{current.timecode === "time-of-day" ? "Mounted DNGs and WAVs start at the time of day each clip was recorded, on the phone's clock, so Resolve can sync clips from several phones or an audio recorder by timecode. Sends from now on use it; media already mounted keeps its timecode." : "Mounted DNGs and WAVs start at 00:00:00:00."}</p>
          {#if current.timecode === "time-of-day"}<ChoiceGroup label="Time zone" choices={zones} value={current.timecode_zone ?? "computer"} onchange={(timecode_zone) => save({ timecode_zone })} />{/if}
          {#if fieldProblem("timecode") ?? fieldProblem("timecode_zone")}<ErrorNote error={(fieldProblem("timecode") ?? fieldProblem("timecode_zone"))!} />{/if}
        </div>
      </div>
      <div class="resolve-choice">
        <span class="field-title">Keywords</span>
        <div>
          <div class="option"><label class="check"><input type="checkbox" role="switch" checked={current.resolve_keywords} onchange={(event) => void changeFlag(event.currentTarget, "resolve_keywords")} />Add measured keywords</label><InfoTip label="Add measured keywords" text="Adds the movement measured from each clip's gyro log (Still, Pan or tilt, Moving or Handheld), and Motion blur when the clip has a second or more of it, to the keywords Resolve receives, after your own. Smart Bins can then gather clips by movement. Your saved keywords are not changed." /></div>
          {#if fieldProblem("resolve_keywords")}<ErrorNote error={fieldProblem("resolve_keywords")!} />{/if}
        </div>
      </div>
    </section>

    <section class="card" aria-labelledby="system-settings">
      <header class="section-heading"><span class="section-icon"><Icon name="settings" /></span><h2 id="system-settings">System</h2><span>Desktop integration</span></header>
      <div class="columns">
        <div class="block"><FileManagerMenu /></div>
        <div class="block"><FileAssociation /></div>
      </div>
    </section>

    <section class="card" aria-labelledby="processing-settings">
      <header class="section-heading"><span class="section-icon"><Icon name="develop" /></span><h2 id="processing-settings">Processing</h2><span>RAW handling and preview performance</span></header>
      <div class="columns">
        <fieldset class="block">
          <legend>RAW processing</legend>
          <div class="option"><label class="check"><input type="checkbox" role="switch" checked={current.quad_bayer_remosaic ?? true} onchange={(event) => void changeFlag(event.currentTarget, "quad_bayer_remosaic")} />Quad Bayer Remosaic</label><InfoTip label="Quad Bayer Remosaic" text="Automatically remosaic marked or detected Quad Bayer footage using its recorded GBRG, RGGB, GRBG or BGGR pattern. Ordinary Bayer footage stays unchanged. Applies to reopened clips and new exports and mounts; existing exports and mounted media keep their original settings." /></div>
          {#if fieldProblem("quad_bayer_remosaic")}<ErrorNote error={fieldProblem("quad_bayer_remosaic")!} />{/if}
        </fieldset>
        <fieldset class="block">
          <legend>Preview performance</legend>
          <div class="option"><label class="check"><input type="checkbox" role="switch" checked={current.preview_gpu ?? true} onchange={(event) => void changeFlag(event.currentTarget, "preview_gpu")} />Use the GPU for fast previews</label><InfoTip label="Preview GPU" text="Uses a compatible GPU for fast previews. Unavailable GPUs use the CPU automatically. Applies when a clip is opened." /></div>
          {#if fieldProblem("preview_gpu")}<ErrorNote error={fieldProblem("preview_gpu")!} />{/if}
          <div class="number-field"><div class="field-title"><label for="preview-cache">Scrub cache (MiB)</label><InfoTip label="Scrub cache" text="Keeps nearby scrub frames and prepares forward playback frames while paused. 0 disables the cache. Applies when a clip is opened. Up to 2048 MiB." /></div><input id="preview-cache" type="number" min="0" max="2048" value={current.preview_cache_mib} onchange={(event) => changeCache(event.currentTarget)} /></div>
          {#if fieldProblem("preview_cache_mib")}<ErrorNote error={fieldProblem("preview_cache_mib")!} />{/if}
        </fieldset>
      </div>
    </section>

    <section class="card" aria-labelledby="debug-settings">
      <header class="section-heading"><span class="section-icon"><Icon name="settings" /></span><h2 id="debug-settings">Debug</h2></header>
      <div class="option"><label class="check"><input type="checkbox" role="switch" checked={current.show_preview_stats ?? false} onchange={(event) => void changeFlag(event.currentTarget, "show_preview_stats")} />Show preview performance statistics</label><InfoTip label="Preview performance statistics" text="Shows playback frames per second, frame processing time, preview dimensions and the processing backend, and the preview cache panel below the viewer. Hidden by default." /></div>
      {#if fieldProblem("show_preview_stats")}<ErrorNote error={fieldProblem("show_preview_stats")!} />{/if}
    </section>
  {/if}
  <CacheManagement />
  <ResetControls onpreferences={async () => { const result = await api.settings(); if (result.ok) show(result.data); }} />
</section>

<style>
  .screen { max-width: 1240px; width: 100%; padding: 28px; box-sizing: border-box; }
  .page-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 22px; }
  h1 { margin: 0 0 5px; font-size: 24px; }
  .page-heading p { margin: 0; font-size: 13px; color: var(--muted); }
  .save-state { color: var(--muted); font-size: 12px; text-align: right; }
  .save-state.failed { color: var(--fail); }
  .card { margin-bottom: 18px; padding: 20px; border: 1px solid var(--line); border-radius: 12px; background: var(--card); }
  .section-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; padding-bottom: 16px; margin-bottom: 18px; border-bottom: 1px solid var(--line); }
  .section-heading h2 { margin: 0; font-size: 17px; font-weight: 600; }
  .section-heading > span:last-child { color: var(--muted); font-size: 12px; }
  .section-icon { display: inline-flex; color: var(--accent); }
  .columns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .block { min-width: 0; margin: 0; padding: 0 24px 0 0; border: 0; }
  .block + .block { padding: 0 0 0 24px; border-left: 1px solid var(--line); }
  legend { display: flex; align-items: center; gap: 4px; padding: 0; margin-bottom: 12px; font-size: 13px; font-weight: 600; }
  h3 { margin: 20px 0 8px; font-size: 13px; font-weight: 600; }
  .mount-folder { margin-top: 20px; }
  .field-title { display: flex; align-items: center; gap: 4px; min-height: 24px; margin-bottom: 8px; font-weight: 600; font-size: 13px; }
  .folders { margin: 0 0 10px; padding: 0; list-style: none; }
  .folders li { display: flex; align-items: center; gap: 10px; padding: 5px 8px; min-height: 36px; border: 1px solid var(--line); border-radius: 7px; margin-bottom: 5px; }
  .folders li :global(svg) { flex-shrink: 0; width: 17px; height: 17px; color: var(--muted); }
  .path { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono); font-size: 12px; }
  .empty { color: var(--muted); font-size: 12px; }
  .row { display: flex; gap: 8px; }
  .row input { flex: 1; min-width: 0; }
  .option { display: flex; align-items: center; gap: 5px; min-height: 32px; font-size: 13px; }
  .resolve-choice { display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 16px; max-width: 720px; }
  .resolve-choice + .resolve-choice { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--line); }
  .resolve-choice .field-title { align-self: start; min-height: 32px; margin: 0; }
  .resolve-choice :global(.list) { margin-left: -11px; }
  .choice-help { margin: 8px 0 0; color: var(--muted); font-size: 12px; line-height: 1.45; }
  .check { display: flex; align-items: center; gap: 9px; }
  .check input { flex: 0 0 auto; }
  .number-field { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 12px; }
  .number-field .field-title { margin: 0; }
  .number-field input { width: 136px; max-width: 100%; }
  .unreadable p { margin-top: 0; }
  @media (max-width: 900px) {
    .screen { padding: 20px; }
    .columns { grid-template-columns: minmax(0, 1fr); }
    .block { padding: 0; }
    .block + .block { padding: 18px 0 0; margin-top: 18px; border-left: 0; border-top: 1px solid var(--line); }
    .section-heading > span:last-child { flex-basis: 100%; padding-left: 32px; }
  }
  @media (max-width: 540px) {
    .resolve-choice { grid-template-columns: minmax(0, 1fr); gap: 6px; }
    .screen { padding: 14px; }
    .card { padding: 16px; }
    .page-heading { align-items: flex-start; }
    .save-state { max-width: 100px; }
    .add-folder { flex-wrap: wrap; }
    .add-folder input { flex-basis: 100%; }
  }
</style>
