<script lang="ts">
  import { onDestroy } from "svelte";
  import { api, type ApiError, type Schemas } from "../lib/api";
  import { bridge } from "../lib/bridge";
  import CameraCalibrationGuide from "./CameraCalibrationGuide.svelte";
  import ErrorNote from "./ErrorNote.svelte";
  import ReportActions from "./ReportActions.svelte";

  let open = $state(false);
  let guide = $state(false);
  let guideLog: Record<string, unknown> | null = null;
  let profiles = $state<Schemas["CameraProfileEntry"][]>([]);
  let clips = $state<Schemas["ClipSummary"][]>([]);
  let captures = $state<Schemas["CaptureRecording"][]>([]);
  let choices = $derived([
    ...clips.map(clip => ({ id: clip.id!, path: clip.path, name: clip.name, camera: clip.camera,
      width: clip.width, height: clip.height, gyro: clip.gyro })),
    ...captures.map(clip => ({ ...clip, id: `file:${clip.path}` })),
  ]);
  let selected = $state("");
  let gyroSelected = $state("");
  let localPath = $state("");
  let gyroPath = $state("");
  let name = $state("");
  let brand = $state("");
  let camera = $state("");
  let lens = $state("");
  let setting = $state("");
  let author = $state("");
  let model = $state<"opencv_standard" | "opencv_fisheye">("opencv_standard");
  let busy = $state(false);
  let installing = $state(false);
  let installed = $state(false);
  let deleteName = $state<string | null>(null);
  let deleting = $state(false);
  let problem = $state<ApiError | null>(null);
  let result = $state<Schemas["LensCaptureResult"] | null>(null);
  let captureName = $state("");
  let controller: AbortController | null = null;
  let notice = $state("");
  let quality = $derived((result?.document.mcrawsome as { quality?: { rms_px: number; held_out_rms_px: number; training_views: number; held_out_views: number; coverage_fraction: number } } | undefined)?.quality);
  let nameValid = $derived(/^[a-z0-9][a-z0-9_]*$/.test(name) && name.length <= 100);

  async function load(): Promise<void> {
    const results = await Promise.allSettled([api.captureProfiles(), api.library(), api.openedClips()]);
    const [saved, library, opened] = results;
    if (saved.status === "fulfilled" && saved.value.ok) profiles = saved.value.data.profiles;
    else if (saved.status === "fulfilled" && !saved.value.ok) problem = saved.value;
    const found: Schemas["ClipSummary"][] = [];
    for (const response of [library, opened]) {
      if (response.status === "fulfilled" && response.value.ok) found.push(...response.value.data.clips);
    }
    clips = [...new Map(found.filter(clip => clip.id && clip.status === "ready").map(clip => [clip.id, clip])).values()];
  }
  function expand(): void { open = true; void load(); }
  function selectClip(): void {
    const clip = choices.find(clip => clip.id === selected);
    if (!name && clip) name = `${clip.camera || "camera"}_${clip.width}x${clip.height}_lens`.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
    if (!camera && clip?.camera) camera = clip.camera;
  }
  async function addRecording(paths: string[], target: "lens" | "gyro" = "lens"): Promise<void> {
    if (!paths.length) return;
    problem = null;
    const added = await api.openCaptureRecording(paths[0]!);
    if (!added.ok) { problem = added; return; }
    const chosen = added.data;
    captures = [...captures.filter(clip => clip.path !== chosen.path), chosen];
    const identity = `file:${chosen.path}`;
    if (chosen) {
      if (target === "gyro") {
        if (!chosen.gyro) { notice = "No gyro data found. Put the matching .gcsv beside the RAW file and refresh recordings."; return; }
        gyroSelected = identity; gyroPath = "";
      } else { selected = identity; selectClip(); localPath = ""; }
    }
  }
  async function browse(target: "lens" | "gyro" = "lens"): Promise<void> {
    try { await addRecording(await $bridge?.api.choose_files?.() ?? [], target); }
    catch { problem = { ok: false, code: "choose-file-failed", message: "Could not open the recording dialog" }; }
  }
  function closeGuide(completed: boolean, log: Record<string, unknown>): void {
    guide = false; guideLog = log;
    notice = completed ? "Capture complete. Copy the original recording to this computer, choose it below, then analyse." : "Guide closed. You can reopen it or analyse a recording you already captured.";
  }
  async function analyse(): Promise<void> {
    if (!selected || !nameValid || busy || deleting) return;
    problem = null; result = null; installed = false; busy = true; notice = "";
    controller = new AbortController();
    const capture = captures.find(clip => `file:${clip.path}` === selected);
    const gyroCapture = captures.find(clip => `file:${clip.path}` === gyroSelected);
    const response = await api.analyseCameraProfile({ clip_id: capture ? null : selected, path: capture?.path ?? null,
      name, camera_brand: brand, camera_model: camera, lens_model: lens, camera_setting: setting,
      calibrated_by: author, model, gyro_clip_id: gyroCapture ? null : gyroSelected || null,
      gyro_path: gyroCapture?.path ?? null }, controller.signal);
    busy = false; controller = null;
    if (!response.ok) { if (response.code !== "paused") problem = response; return; }
    result = response.data; captureName = response.data.profile.name;
    if (guideLog && result.document.mcrawsome && typeof result.document.mcrawsome === "object") {
      (result.document.mcrawsome as Record<string, unknown>).capture_guide = guideLog;
    }
  }
  function cancel(): void { controller?.abort(); }
  async function install(): Promise<void> {
    if (!result?.passed || installing || installed || deleting) return;
    installing = true; problem = null;
    const saved = await api.saveCaptureProfile(result.profile);
    installing = false;
    if (!saved.ok) { problem = saved; return; }
    installed = true;
    profiles = [saved.data, ...profiles];
  }
  async function remove(name: string): Promise<void> {
    if (deleting || busy || installing) return;
    deleting = true; problem = null;
    const deleted = await api.deleteCameraProfile(name);
    deleting = false;
    if (!deleted.ok) { problem = deleted; return; }
    profiles = deleted.data.profiles;
    deleteName = null;
    if (result?.profile.name === name) { result = null; installed = false; }
    notice = `Deleted camera profile ${name}. You can create a new profile with the same name.`;
  }
  onDestroy(cancel);
</script>

<section class="camera-profiles" aria-labelledby="camera-profiles-heading">
  <header><div><h2 id="camera-profiles-heading">Camera profiles</h2><p>Manage saved profiles or measure your lens with a guided screen target.</p></div><button class="secondary" onclick={open ? () => { open = false; } : expand} disabled={busy || deleting || installing}>{open ? "Close profiles" : "Manage profiles"}</button></header>
  {#if open}
    {#if profiles.length}
      <details class="installed-profiles" open>
        <summary>Installed profiles ({profiles.length})</summary>
        <ul>{#each profiles as entry}
          <li><div><strong>{entry.profile.name}</strong> · {entry.user ? "User" : "Built in"}{#if entry.profile.lens_calibration} · measured lens{/if}</div>
            {#if entry.user}
              {#if deleteName === entry.profile.name}
                <div class="profile-actions"><span>Delete this profile?</span><button class="danger" onclick={() => void remove(entry.profile.name)} disabled={deleting || busy || installing}>{deleting ? "Deleting…" : "Delete profile"}</button><button class="secondary" onclick={() => { deleteName = null; }} disabled={deleting}>Cancel</button></div>
              {:else}
                <button class="secondary" aria-label={`Delete profile ${entry.profile.name}`} onclick={() => { deleteName = entry.profile.name; }} disabled={deleting || busy || installing}>Delete</button>
              {/if}
            {/if}
          </li>
        {/each}</ul>
      </details>
    {/if}
    <div class="intro">
      <div><span class="eyebrow">1 · CAPTURE</span><h3>A target on this screen. A recording on your phone.</h3><p>Use the physical lens, resolution, sensor crop and frame rate you want to profile. Turn off electronic stabilisation, automatic lens switching and digital zoom. Clean the lens, lock exposure and white balance, then lock focus so the board stays sharp at both distances.</p><p>Keep this display flat and still. Use a bright screen without glare, moiré or flickering bands. The timed guide includes four sweeps, roll and oblique views at two distances. Allow about five minutes; spoken prompts are optional.</p><button onclick={() => { guide = true; }}>Show calibration target</button></div>
      <aside><strong>Recommended recording settings</strong><ul><li>Use your intended recording mode. For the sample Pixel 8 profile: 2048 × 1536 at 24 fps. Avoid changing crop, binning or frame rate mid-recording.</li><li>Start around 1/125–1/250 s for sharp corners. Check a short test for dark bands or flicker. If present, try a full display refresh period (for example 1/60 s on a 60 Hz display) and move more slowly.</li><li>Use the lowest practical ISO without clipping the white squares. Lock focus, exposure and white balance; keep the board sharp at both distances.</li><li>Use your largest flat display to calibrate at a useful focus distance. A curved screen changes the target geometry.</li></ul><p>Transfer the original .mcraw, .mp4, .mov or .mkv file after capture. Keep its matching .gcsv file beside a RAW gyro recording.</p></aside>
    </div>
    <form onsubmit={(event) => { event.preventDefault(); void analyse(); }}>
      <span class="eyebrow">2 · ANALYSE</span><h3>Choose the original recording</h3><p>Calibration accepts RAW and readable video, including linear, SDR and Log recordings. Files opened here are used only for calibration.</p>
      <div class="recording-row"><label>Calibration recording<select bind:value={selected} onchange={selectClip} disabled={busy}><option value="">Choose a recording…</option>{#each choices as clip (clip.id)}<option value={clip.id!}>{clip.name} · {clip.width} × {clip.height}</option>{/each}</select></label><button type="button" class="secondary" onclick={() => void load()} disabled={busy}>Refresh recordings</button>{#if $bridge?.api.choose_files}<button type="button" class="secondary" onclick={() => void browse()} disabled={busy}>Browse…</button>{/if}</div>
      <div class="recording-row"><label>Or open a local recording<input aria-label="Local calibration recording path" bind:value={localPath} placeholder="/path/to/calibration.mcraw" disabled={busy} /></label><button type="button" class="secondary" onclick={() => void addRecording([localPath.trim()])} disabled={busy || !localPath.trim()}>Open recording</button></div>
      <div class="fields">
        <label>Profile name<input bind:value={name} required pattern="[a-z0-9][a-z0-9_]*" maxlength="100" placeholder="pixel_8_main_2048x1536_24fps" disabled={busy} /><small>Lowercase letters, numbers and underscores. Use a separate name for each recording mode.</small></label>
        <label>Lens model<select bind:value={model} disabled={busy}><option value="opencv_standard">Standard lens (radial + tangential)</option><option value="opencv_fisheye">Fisheye lens</option></select><small>Choose the physical lens model. A normal phone main lens usually uses Standard.</small></label>
        <label>Camera brand<input bind:value={brand} maxlength="100" placeholder="Google" disabled={busy} /></label>
        <label>Camera model<input bind:value={camera} required maxlength="100" placeholder="Pixel 8" disabled={busy} /></label>
        <label>Physical lens<input bind:value={lens} maxlength="100" placeholder="25 mm equivalent · main camera" disabled={busy} /></label>
        <label>Recording mode<input bind:value={setting} maxlength="200" placeholder="MotionCam · full sensor · 4:3" disabled={busy} /></label>
        <label>Calibrated by<input bind:value={author} maxlength="100" placeholder="Your name (optional)" disabled={busy} /></label>
      </div>
      <details class="gyro-option"><summary>Add gyro and rolling-shutter calibration (optional)</summary><p>Record a separate 30–60 second clip of a static, distant, detailed scene with the same lens and recording mode. Turn stabilisation off, use a short exposure and record slow pans, tilts and rolls in both directions. Enable gyro logging. Keep the matching .gcsv beside a RAW file.</p><label>Gyro calibration recording<select bind:value={gyroSelected} disabled={busy}><option value="">Lens geometry only</option>{#each choices.filter(clip => clip.gyro && clip.id !== selected) as clip (clip.id)}<option value={clip.id!}>{clip.name}</option>{/each}</select></label><div class="recording-row"><label>Local gyro recording path<input bind:value={gyroPath} placeholder="/path/to/motion.mcraw" disabled={busy} /></label><button type="button" class="secondary" onclick={() => void addRecording([gyroPath.trim()], "gyro")} disabled={busy || !gyroPath.trim()}>Open gyro recording</button>{#if $bridge?.api.choose_files}<button type="button" class="secondary" onclick={() => void browse("gyro")} disabled={busy}>Browse gyro recording…</button>{/if}</div></details>
      <div class="actions"><button type="submit" disabled={busy || deleting || !selected || !nameValid}>{busy ? "Analysing recording…" : "Analyse and create JSON"}</button>{#if busy}<button type="button" class="secondary" onclick={cancel}>Cancel analysis</button>{/if}</div>
      {#if busy}<p role="status">Finding sharp checkerboards, selecting varied views, fitting lens geometry and testing independent views. This can take several minutes.</p>{/if}
    </form>
    {#if notice}<p class="notice" role="status">{notice}</p>{/if}
    {#if problem}<ErrorNote error={problem} />{/if}
    {#if result}
      <section class="result" aria-label="Calibration result">
        <span class="eyebrow">3 · REVIEW &amp; SAVE</span><h3>{result.passed ? "JSON profile ready" : "Capture needs improvement"}</h3>
        {#if quality}<dl><div><dt>Fit error</dt><dd>{quality.rms_px.toFixed(3)} px</dd></div><div><dt>Independent validation</dt><dd>{quality.held_out_rms_px.toFixed(3)} px</dd></div><div><dt>Views used / held out</dt><dd>{quality.training_views} / {quality.held_out_views}</dd></div><div><dt>Image coverage</dt><dd>{Math.round(quality.coverage_fraction * 100)}%</dd></div></dl>{/if}
        {#if result.issues.length}<ul>{#each result.issues as issue}<li>{issue}</li>{/each}</ul>{/if}
        <p>The JSON includes lens parameters, the camera matrix, distortion, original recording dimensions, per-view errors, independent validation, coverage and capture metadata. Unmeasured values stay null or are listed as unmeasured.</p>
        <ReportActions value={result.document} filename={`${captureName}.json`} label="camera profile" />
        <button class="secondary" onclick={() => void install()} disabled={!result.passed || installing || installed || deleting}>{installed ? "Camera profile added" : installing ? "Adding…" : "Add profile to MCRAWsome"}</button>
        {#if !result.passed}<p>Save the JSON as a diagnostic report, then capture again before adding the profile.</p>{/if}
        <p class="limitation">The screen target measures lens geometry. The optional distant-scene recording supplies gyro axes, sync and readout when its motion fit is trusted. Unmeasured values remain unknown. Lens geometry is currently saved for interchange; MCRAWsome does not yet apply lens correction to developed pixels.</p>
      </section>
    {/if}

  {/if}
</section>
{#if guide}<CameraCalibrationGuide onclose={closeGuide} />{/if}

<style>
  .camera-profiles { margin-bottom: 18px; padding: 20px; border: 1px solid var(--line); border-radius: 12px; background: var(--card); }
  header { display: flex; align-items: center; justify-content: space-between; gap: 18px; }
  h2 { margin: 0 0 5px; font-size: 17px; font-weight: 600; } h3 { margin: 7px 0 12px; font-size: 16px; }
  p { margin: 0 0 12px; color: var(--muted); font-size: 13px; line-height: 1.6; }
  header p { margin: 0; }
  .intro { display: grid; grid-template-columns: 1.6fr 1fr; gap: 30px; margin-top: 24px; padding-top: 22px; border-top: 1px solid var(--line); }
  aside { padding: 20px; border-radius: 8px; background: var(--bg); font-size: 13px; } aside strong { display: block; margin-bottom: 10px; }
  .eyebrow { font: 10px var(--mono); color: var(--accent); letter-spacing: .08em; }
  form { border-top: 1px solid var(--line); margin-top: 24px; padding-top: 22px; }
  .recording-row { display: flex; align-items: flex-end; gap: 10px; margin: 12px 0; } .recording-row label { flex: 1; }
  .fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 20px 0; }
  label { display: flex; flex-direction: column; gap: 7px; font-size: 12px; } input, select { width: 100%; box-sizing: border-box; }
  small { color: var(--muted); font-size: 11px; line-height: 1.5; }
  .actions { display: flex; gap: 10px; margin-bottom: 12px; }
  .result { margin-top: 22px; padding: 20px; border: 1px solid var(--line); border-radius: 8px; }
  dl { display: grid; grid-template-columns: repeat(4, 1fr); gap: 18px; margin: 18px 0; } dt { color: var(--muted); font-size: 11px; } dd { margin: 5px 0; font: 16px var(--mono); }
  .limitation { margin: 16px 0 0; font-size: 11px; } .notice { margin-top: 16px; }
  ul { padding-left: 20px; font-size: 12px; line-height: 1.8; } details { margin-top: 22px; font-size: 12px; }
  .installed-profiles ul { padding: 0; list-style: none; }
  .installed-profiles li { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px 16px; padding: 10px 0; border-bottom: 1px solid var(--line); }
  .installed-profiles strong { overflow-wrap: anywhere; }
  .profile-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
  .danger { background: var(--fail); color: var(--bg); }
  @media (max-width: 800px) { .intro { grid-template-columns: 1fr; gap: 16px; } dl { grid-template-columns: 1fr 1fr; } .recording-row { flex-wrap: wrap; } .recording-row label { flex-basis: 100%; } }
  @media (max-width: 540px) { header { align-items: flex-start; } .fields { grid-template-columns: 1fr; } .camera-profiles { padding: 16px; } }
</style>
