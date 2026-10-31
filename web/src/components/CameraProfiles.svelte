<script lang="ts">
  import { onDestroy } from "svelte";
  import { api, type ApiError, type Schemas } from "../lib/api";
  import { bridge } from "../lib/bridge";
  import ErrorNote from "./ErrorNote.svelte";
  let opened = $state(false);
  let adding = $state(false);
  let entries = $state<Schemas["ProfileEntry"][]>([]);
  let problem = $state<ApiError | null>(null);
  let path = $state("");
  let model = $state<"opencv_standard" | "opencv_fisheye">("opencv_standard");
  let result = $state<Schemas["LensAnalysis"] | null>(null);
  let name = $state("");
  let busy = $state(false);
  let saving = $state(false);
  let message = $state("");
  let operation: AbortController | null = null;
  onDestroy(() => operation?.abort());
  async function manage(): Promise<void> {
    opened = !opened;
    if (!opened) return;
    const response = await api.cameraProfiles();
    if (response.ok) entries = response.data.profiles; else problem = response;
  }
  function reset(): void { result = null; message = ""; }
  async function choose(): Promise<void> {
    try {
      const selected = await $bridge?.api.choose_files?.();
      if (selected?.length) { path = selected[0]!; reset(); }
    } catch { problem = { ok: false, code: "input-unreadable", message: "The recording dialog could not be opened" }; }
  }
  async function analyse(): Promise<void> {
    if (busy || !path.trim()) return;
    busy = true; problem = null; reset(); operation = new AbortController();
    try {
      const measured = await api.analyseLens(path.trim(), model, operation.signal);
      if (measured.ok) { result = measured.data; name = measured.data.profile.name; }
      else if (measured.code === "paused") message = "Analysis cancelled.";
      else problem = measured;
    } finally { busy = false; operation = null; }
  }
  async function save(): Promise<void> {
    if (!result || saving) return;
    saving = true; problem = null;
    try {
      const response = await api.saveCameraProfile({ ...result.profile, name: name.trim() });
      if (!response.ok) { problem = response; return; }
      message = `Saved ${response.data.path}`; result = null; adding = false;
      const listed = await api.cameraProfiles();
      if (listed.ok) entries = listed.data.profiles;
    } finally { saving = false; }
  }
  async function download(event: MouseEvent, profile?: string): Promise<void> {
    const native = profile ? $bridge?.api.save_camera_profile : $bridge?.api.save_calibration_target;
    if (!native) return;
    event.preventDefault();
    try {
      const saved = profile ? await $bridge!.api.save_camera_profile!(profile) : await $bridge!.api.save_calibration_target!();
      message = saved ? "File saved" : "Save cancelled";
    } catch { problem = { ok: false, code: "output-unwritable", message: "Could not save this file" }; }
  }
</script>
<p class="intro">Create a measured lens profile for a phone and camera module using a short target recording.</p>
<button class="secondary" disabled={busy} aria-expanded={opened} onclick={() => void manage()}>Manage camera profiles</button>
{#if problem}<ErrorNote error={problem} />{/if}
{#if message}<p role="status">{message}</p>{/if}
{#if opened}
  <div class="profiles">
    {#each entries as entry (`${entry.user}:${entry.profile.name}`)}
      <article>
        <strong>{entry.profile.name}</strong><span>{entry.user ? "User profile" : "Built-in profile"}</span>
        <p>{entry.profile.match.device_model ?? "Any phone"} · Camera {entry.profile.match.camera_id ?? "any"} · {entry.profile.match.width ?? "any"} × {entry.profile.match.height ?? "any"}{entry.profile.match.fps ? ` · ${entry.profile.match.fps} fps` : ""}</p>
        <p>{entry.profile.lens_calibration ? `Measured lens · ${entry.profile.lens_calibration.image_count} views` : "No measured lens geometry"}</p>
        <p>{entry.profile.gcsv_axes && entry.profile.gcsv_sync_ms != null ? "Existing RAW gyro timing available" : "RAW gyro timing not calibrated"}</p>
        <a href={`/api/v1/camera-profiles/${encodeURIComponent(entry.profile.name)}.toml`} download onclick={(event) => void download(event, entry.profile.name)} aria-label={`Download ${entry.profile.name}`}>Download profile</a>
      </article>
    {:else}<p>No camera profiles found.</p>{/each}
  </div>
  <button class="secondary" disabled={busy} onclick={() => { adding = !adding; problem = null; reset(); }}>Add camera profile</button>
  {#if adding}
    <div class="wizard">
      <h3>1. Record the calibration target</h3>
      <a href="/api/v1/camera-profiles/target.svg" download onclick={(event) => void download(event)}>Download calibration target</a>
      <ol>
        <li>Print the target without stretching it and fix it flat to a wall or rigid board.</li>
        <li>Choose the phone camera, resolution and frame rate you want to calibrate. Lock focus and exposure. Turn off lens and electronic stabilisation.</li>
        <li>Move the phone while the target stays still. Record 20–30 seconds, moving nearer, further, left, right, up and down. Tilt and roll gently.</li>
        <li>Keep every square visible. Show the board near each image edge and at varied angles. Avoid blur, glare and bending the paper.</li>
      </ol>
      <h3>2. Analyse the recording</h3>
      <form onsubmit={(event) => { event.preventDefault(); void analyse(); }}>
        <label>Target recording path<input type="text" bind:value={path} disabled={busy} oninput={reset} placeholder="/path/to/calibration.mcraw" /></label>
        {#if $bridge?.api.choose_files}<button type="button" class="secondary" disabled={busy} onclick={() => void choose()}>Choose recording…</button>{/if}
        <label>Lens model<select bind:value={model} disabled={busy} onchange={reset}><option value="opencv_standard">Standard</option><option value="opencv_fisheye">Fisheye</option></select></label>
        <button type="submit" disabled={busy || !path.trim()}>Analyse recording</button>
        {#if busy}<button type="button" class="secondary" onclick={() => operation?.abort()}>Cancel analysis</button><p role="status">Analysing up to 60 target views…</p>{/if}
      </form>
      {#if result}
        <h3>3. Review and save</h3>
        <p class="identity">{result.profile.match.device_model} · Camera {result.profile.match.camera_id}</p>
        <p>{result.profile.match.width} × {result.profile.match.height} · {result.profile.match.fps?.toFixed(3)} fps</p>
        <p>{result.detected_images} target views from {result.sampled_images} samples · Fit error {result.profile.lens_calibration?.rms_error?.toFixed(3)} pixels at {result.profile.lens_calibration?.width} × {result.profile.lens_calibration?.height}</p>
        <label>Profile name<input bind:value={name} pattern="[a-z0-9][a-z0-9_]*" disabled={saving} /></label>
        <p>Use lowercase letters, numbers and underscores. Existing profiles are kept; choose a new name to save another calibration.</p>
        {#each result.notes as note}<p>{note}</p>{/each}
        <button disabled={saving || !/^[a-z0-9][a-z0-9_]*$/.test(name)} onclick={() => void save()}>Save camera profile</button>
      {/if}
    </div>
  {/if}
{/if}
<style>
  p, li { font-size: 13px; line-height: 1.55; color: var(--muted); }
  .intro { margin-top: 0; }
  .profiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr)); gap: 12px; margin: 16px 0; }
  article, .wizard { padding: 16px; border: 1px solid var(--line); border-radius: 8px; }
  article strong { display: block; overflow-wrap: anywhere; font-size: 13px; }
  article span { font-size: 11px; color: var(--muted); }
  article p { margin: 5px 0; }
  a { color: var(--accent); font-size: 13px; }
  .wizard { margin-top: 16px; }
  h3 { font-size: 15px; margin: 20px 0 12px; }
  h3:first-child { margin-top: 0; }
  form { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; }
  label { display: grid; gap: 6px; font-size: 13px; }
  input { min-width: 0; max-width: 100%; box-sizing: border-box; }
  form > label:first-child { flex: 1 1 260px; min-width: 0; }
  form > label:first-child input { width: 100%; }
  .identity { color: var(--text); font-weight: 600; }
</style>
