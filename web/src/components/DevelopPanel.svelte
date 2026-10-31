<script lang="ts">
  import type { ApiError, RecipeValues, Schemas } from "../lib/api";
  import RawControls from "./RawControls.svelte";
  import Slider from "./Slider.svelte";
  import InfoTip from "./InfoTip.svelte";
  import InputColour from "./InputColour.svelte";
  import SectionCard from "./SectionCard.svelte";
  import { setSectionOpen } from "../lib/sections";

  let { recipe, values, sources = {}, raw = null, croptools, cropediting = null, gyro = false, blurredSeconds = null, orientationHint = null, colourNote = null, detectedTransfer = null, onclear, problem, onchange, oncolour }: {
    sources?: Record<string, string>; values: RecipeValues; raw?: { blacks: number[]; white: number } | null;
    croptools?: import("../lib/crop-edit").CropTools; cropediting?: import("../lib/crop-edit").CropEdit | null;
    gyro?: boolean; blurredSeconds?: number | null;
    orientationHint?: import("../lib/api").Schemas["OrientationHint"] | null;
    colourNote?: string | null; detectedTransfer?: string | null;
    recipe: RecipeValues; problem: ApiError | null;
    onclear: (fields: string[]) => void;
    onchange: (section: string, field: string, value: unknown) => void;
    oncolour: (values: Record<string, unknown>) => void;
  } = $props();
  const controlId = $props.id();
  const error = (field: string): string | undefined => problem?.field === field ? problem.message : undefined;
  let stabilise = $derived(recipe.motion?.stabilise !== "off");
  let mode = $derived(String(recipe.motion?.mode ?? "smooth"));
  let rowsOn = $derived(Number(recipe.motion?.bands ?? 36) > 1);
  let rate = $derived(String(recipe.output?.frame_rate ?? "keep"));
  let reviewCategories = $derived((recipe.motion?.review_categories ?? {}) as Partial<Schemas["ReviewCategories"]>);
  const REVIEW = [
    { key: "shake", label: "Possible shake", colour: "#ef5350" },
    { key: "focus", label: "Frequent focus changes", colour: "#ab7bde" },
    { key: "recorded_tilt", label: "Recorded tilt", colour: "#ef8db7" },
    { key: "remaining_tilt", label: "Remaining tilt", colour: "#4dd0e1" },
    { key: "clipping", label: "Highlight clipping", colour: "#f2ce50" },
    { key: "low_signal", label: "Low recorded signal", colour: "#64a5ed" },
    { key: "gaps", label: "Recording gap", colour: "#d2b48c" },
    { key: "blur", label: "Motion blur", colour: "#7cc576" },
  ] as const;
  const RATES = ["24", "25", "30", "50", "60", "120"];
  // At 1.00 there is no margin to turn the frame within, so no correction fits (motion.crop in the recipe schema).
  const MIN_CROP = 1.05;
  $effect(() => {
    const field = problem?.field;
    if (field?.startsWith("motion.")) setSectionOpen("stabilise", true);
    else if (field === "develop.mount_proxy") setSectionOpen("resolve", true);
  });
  // What each folded section says it is set to.
  const ORIENTATIONS: Record<number, string> = { 0: "As recorded", 90: "Rotate 90° clockwise", 180: "Rotate 180°", 270: "Rotate 90° anticlockwise" };
  const MODES: Record<string, string> = { smooth: "Smooth", rolling_shutter: "Rolling shutter only", tripod: "Virtual tripod", pan: "Pan freely" };
  const PROXIES: Record<string, string> = { full: "Proxy off", half: "Proxy 2×", quarter: "Proxy 4×", eighth: "Proxy 8×" };
  const LOG_RATES: Record<string, string> = { keep: "Recorded / VFR nominal", integer: "Nearest whole rate", "drop-frame": "Nearest NTSC rate", average: "Average", median: "Median" };
  let rotation = $derived(Number(recipe.develop?.rotation ?? 0));
  let orientationWrong = $derived(orientationHint != null && rotation !== orientationHint.suggested_rotation);
  let sourceSummary = $derived(raw
    ? `${ORIENTATIONS[rotation] ?? `${rotation}°`}${orientationWrong ? " · check orientation" : ""}`
    : `Frame rate: ${LOG_RATES[rate] ?? `${rate} fps`}`);
  let stabiliseSummary = $derived(!gyro ? "No gyro log" : !stabilise ? "Off" : [
    MODES[mode] ?? mode, `crop ${Number(recipe.motion?.crop ?? 1.1).toFixed(2)}`,
    mode === "rolling_shutter" ? null : `strength ${Number(recipe.motion?.strength ?? 1).toFixed(2)}`,
  ].filter(part => part !== null).join(" · "));
  let proxySummary = $derived(PROXIES[String(recipe.develop?.mount_proxy ?? "full")] ?? "Proxy");
  let reviewSummary = $derived(recipe.motion?.markers ? `${REVIEW.filter(category => reviewCategories[category.key] ?? true).length} of ${REVIEW.length} kinds` : "Off");
</script>
<aside aria-label="Resolve settings">
  <SectionCard id="source" title="Source" summary={sourceSummary}>
  {#if raw}
    <label>Orientation <select value={String(rotation)} onchange={(event) => onchange("develop", "rotation", Number(event.currentTarget.value))}>
      <option value="0">As recorded</option><option value="90">Rotate 90° clockwise</option>
      <option value="180">Rotate 180°</option><option value="270">Rotate 90° anticlockwise</option>
    </select></label>
    {#if orientationHint && orientationWrong}
      <p class="orientation-note" role="status">Orientation appears wrong. Gravity near the start suggests
        {ORIENTATIONS[orientationHint.suggested_rotation]}.
        Review the preview before changing it.</p>
    {/if}
  {:else}
    <InputColour develop={recipe.develop ?? {}} note={colourNote} {detectedTransfer} onchange={oncolour} />
    <div class="option-line"><label>Frame rate <select value={rate} onchange={(event) => onchange("output", "frame_rate", Number.isFinite(Number(event.currentTarget.value)) ? Number(event.currentTarget.value) : event.currentTarget.value)}>
      <option value="keep">Recorded / VFR nominal</option><option value="integer">Nearest whole rate</option><option value="drop-frame">Nearest NTSC rate</option>
      <option value="average">Average</option><option value="median">Median</option>
      {#each RATES as value}<option {value}>{value} fps</option>{/each}
      {#if !["keep", "integer", "drop-frame", "average", "median", ...RATES].includes(rate)}<option value={rate}>{rate} fps</option>{/if}
    </select></label>
    <InfoTip label="Frame rate" text="Variable-rate LOG uses recorded timestamps to repeat or skip pictures on a fixed-rate preview timeline. Audio keeps its recorded speed. This affects LOG playback; native Send to Resolve is available for RAW clips." /></div>
  {/if}
  </SectionCard>
  <SectionCard id="stabilise" title="Stabilise" summary={stabiliseSummary} initiallyOpen>
    {#snippet control()}<input type="checkbox" role="switch" aria-label="Stabilise" disabled={!gyro} checked={stabilise} onchange={(event) => onchange("motion", "stabilise", event.currentTarget.checked ? "auto" : "off")} />{/snippet}
    {#if !gyro}<p class="muted">No gyro log for this clip</p>{/if}
    {#if gyro}<p class="muted option-line">Used by Send to Resolve (Timeline) <InfoTip label="Stabilise" text="Send to Resolve (Timeline) stabilises with these settings when Stabilise is on and the gyro can be trusted. Send again after editing; Resolve's Stabilize button may replace the corrections." /></p>{/if}
    <fieldset class="settings" disabled={!gyro || !stabilise} aria-label="Stabilise settings">
      <div class="option-line"><label>Mode <select value={mode} onchange={(event) => onchange("motion", "mode", event.currentTarget.value)}>
        <option value="smooth">Smooth</option><option value="rolling_shutter">Rolling shutter only</option>
        <option value="tripod">Virtual tripod</option><option value="pan">Pan freely</option>
      </select></label>
      <InfoTip label="Mode" text="Smooth follows the camera's movement, smoothed over the Smoothing time. Rolling shutter only straightens the wobble of the camera's rolling shutter and leaves the framing as shot; it needs Rolling shutter compensation on. Virtual tripod holds one view for the whole clip, as far as the crop allows. Pan freely holds tilt and roll steady and lets the pan follow, smoothed." /></div>
      {#if mode === "rolling_shutter" && !rowsOn}<p class="muted" role="note">Turn on Rolling shutter compensation for this mode.</p>{/if}
      <Slider label="Crop" min={MIN_CROP} max={1.5} value={Number(recipe.motion?.crop ?? 1.1)} error={error("motion.crop")} onchange={(value) => onchange("motion", "crop", value)}
        info="How far Resolve zooms in to hide the frame's moving edges. Stabilising turns each frame to cancel shake, and the crop is the margin it can turn within: at 1.10 the centre 91% of the width and height remains. More crop allows steadier results but loses more of the picture. The outline on the preview shows the frame that remains. The minimum is 1.05 because with no margin nothing can be corrected." />
      <Slider label="Strength" min={0} max={1} value={Number(recipe.motion?.strength ?? 1)} disabled={mode === "rolling_shutter"} error={error("motion.strength")} onchange={(value) => onchange("motion", "strength", value)}
        info="How much of the camera's movement is smoothed away (or, for Virtual tripod and Pan freely, held). 1.00 uses the full Mode; 0.50 removes only fast shake and keeps slower movement; 0.00 keeps the movement as recorded. To straighten only the rolling shutter, choose Rolling shutter only as the Mode. Where a frame needs more correction than the crop allows, strength is reduced there gradually." />
      <Slider label="Smoothing (seconds)" min={0.05} max={5} step={0.05} value={Number(recipe.motion?.smoothing_seconds ?? 0.5)} disabled={mode === "rolling_shutter" || mode === "tripod"} error={error("motion.smoothing_seconds")} onchange={(value) => onchange("motion", "smoothing_seconds", value)}
        info="How slowly the stabilised camera may move, at full strength. Short times follow deliberate pans and walking closely; long times give a steadier, floating look but need more crop for the same movement, so strength is reduced more often." />
      <label><input type="checkbox" role="switch" checked={Number(recipe.motion?.bands ?? 36) > 1} onchange={(event) => onchange("motion", "bands", event.currentTarget.checked ? 36 : 1)} /> Rolling shutter compensation</label>
      <div class="option-line"><label><input type="checkbox" role="switch" checked={recipe.motion?.optical_correction !== false} onchange={(event) => onchange("motion", "optical_correction", event.currentTarget.checked)} /> Image-assisted refinement</label>
      <InfoTip label="Image-assisted refinement" text="Tracks image detail throughout the clip to refine shake the gyro cannot explain. Adds analysis time when sending a timeline. Unreliable image measurements keep the gyro correction. Turn off if moving subjects pull the framing." /></div>
      {#if raw}
        <div class="option-line"><label><input type="checkbox" role="switch" checked={Boolean(recipe.motion?.horizon_lock)} onchange={(event) => onchange("motion", "horizon_lock", event.currentTarget.checked)} /> Horizon Lock</label>
        <InfoTip label="Horizon Lock" text="Levels the horizon in Send to Resolve (Timeline), using accelerometer data and a verified sensor-to-image mapping. The crop limits how far it can level; strong acceleration or an unsupported camera can prevent it. Manual display orientation is retained." /></div>
      {/if}
    </fieldset>
    {#if gyro && stabilise && blurredSeconds}<p class="muted blur-note" role="note">This clip has {blurredSeconds.toFixed(1)} s of motion blur from shake. Stabilising steadies the picture but leaves the blur, which can look smeared; a lower Strength keeps some of the movement that explains it.</p>{/if}
  </SectionCard>
  {#if raw}
    <SectionCard id="resolve" title="Resolve" summary={proxySummary}>
      <div class="proxy-field"><div class="option-line"><label for={`${controlId}-proxy`}>Proxy</label><InfoTip label="Proxy" text="Uses smaller virtual DNGs for editing. Sending to Resolve also mounts and links the full-resolution original. Choose Prefer Proxies for playback, and leave Use proxy media off in Deliver to render from originals. Output dimensions follow your timeline settings. Existing mounts keep their size; send again after changing it." /></div>
      <select id={`${controlId}-proxy`} value={String(recipe.develop?.mount_proxy ?? "full")} onchange={(event) => onchange("develop", "mount_proxy", event.currentTarget.value)}>
        <option value="full">Off (full resolution)</option><option value="half">2× (half width and height)</option><option value="quarter">4× (quarter width and height)</option><option value="eighth">8× (eighth width and height)</option>
      </select></div>
      {#if (recipe.develop?.mount_proxy ?? "full") !== "full"}
        <div class="option-line"><label><input type="checkbox" role="switch" checked={recipe.develop?.mount_first_frame === "full"} onchange={(event) => onchange("develop", "mount_first_frame", event.currentTarget.checked ? "full" : "proxy")} /> Full-size first frame</label><InfoTip label="Full-size first frame" text="Serves the first DNG at full resolution and the rest as proxies, so an editor that sizes a sequence from its first frame lists the clip at its recorded size. For media added from the mount folder; Send to Resolve links full-resolution originals instead." /></div>
      {/if}
      {#if error("develop.mount_proxy")}<p role="alert">{error("develop.mount_proxy")}</p>{/if}
    </SectionCard>
    <SectionCard id="review" title="Review markers" summary={reviewSummary}>
      {#snippet control()}<input type="checkbox" role="switch" aria-label="Review markers" checked={Boolean(recipe.motion?.markers)} onchange={(event) => onchange("motion", "markers", event.currentTarget.checked)} />{/snippet}
      <p class="muted option-line">Suggestions for the editor on Resolve timelines <InfoTip label="Review markers" text="Adds editor review suggestions to Resolve timelines: frequent focus changes, recording gaps, sampled RAW highlight clipping/low signal, severe shake and sustained motion blur from shake (from the gyro log and shutter). Recorded tilt and Remaining tilt use calibrated gravity, with 5-degree warnings sustained for one second. At most 120 RAW frames are sampled; brief events between samples may be missed. Missing or unreliable sensors are skipped. Remaining tilt is available only with Stabilise enabled and measures tilt after correction. Colours: purple focus, pink recorded tilt, cyan remaining tilt, yellow clipping, blue low signal, red shake, sand recording gaps, green motion blur. These suggestions can reflect intentional choices. Bin sends do not include them." /></p>
      <fieldset class="review-categories" disabled={!recipe.motion?.markers} aria-label="Review marker categories">
        {#each REVIEW as category}
          <label style={`--accent: ${category.colour}; --accent-ink: #171717`}>
            <input type="checkbox" role="switch" aria-label={category.label}
              disabled={category.key === "remaining_tilt" && (!gyro || !stabilise)}
              checked={reviewCategories[category.key] ?? true}
              onchange={(event) => onchange("motion", "review_categories", { ...reviewCategories, [category.key]: event.currentTarget.checked })} />
            {category.label}
          </label>
        {/each}
      </fieldset>
    </SectionCard>
    <RawControls {recipe} {values} {sources} onpatch={oncolour} blacks={raw.blacks} white={raw.white} {problem} {onchange} {onclear} {croptools} {cropediting} />
  {/if}
  {#if problem?.field && problem.field !== "develop.exposure" && !problem.field.startsWith("motion.") && !(raw && ["develop.black_level", "develop.white_level", "develop.lens_shading", "develop.lens_shading_mode", "develop.mount_shading", "develop.mount_proxy", "develop.mount_frame_rate", "develop.mount_normalise_exposure", "develop.mount_normalise_seconds", "develop.mount_transfer", "develop.mount_reduce_bits", "develop.mount_crop", "develop.mount_camera_model", "develop.mount_unique_model"].includes(problem.field))}<p role="alert">{problem.message}</p>{/if}
</aside>
<style>
  aside { container-type: inline-size; display: grid; gap: 8px; padding: 10px; background: var(--panel); min-width: 0; }
  .review-categories { display: grid; gap: 8px; border: 0; padding: 0; margin: 0; min-width: 0; }
  @media (forced-colors: none) { .review-categories input { border-color: var(--accent); } }
  .review-categories label { display: flex; align-items: center; gap: 8px; }
  .blur-note { margin: 0; font-size: 11px; }
  .settings { border: 0; padding: 0; margin: 0; min-width: 0; display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 8px; }
  .settings:disabled { opacity: 0.5; }
  .option-line { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; min-width: 0; }
  .proxy-field { display: grid; gap: 6px; min-width: 0; }
  .proxy-field select { width: 100%; min-width: 0; }
  label { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; color: var(--text); font-size: 12px; }
  select { color: var(--text); }
  label:has(input) { justify-content: flex-start; }
  .muted { color: var(--muted); margin: 0; }
  .orientation-note { color: var(--warn); margin: 0; font-size: 12px; }
  @container (max-width: 330px) { .settings { grid-template-columns: minmax(0,1fr); } }
</style>
