<script lang="ts">
  import type { ApiError, RecipeValues } from "../lib/api";
  import InfoTip from "./InfoTip.svelte";
  import SectionCard from "./SectionCard.svelte";
  import { setSectionOpen } from "../lib/sections";
  import Slider from "./Slider.svelte";
  import { ASPECTS, parseCrop } from "../lib/dng-crop";
  import type { CropEdit, CropTools } from "../lib/crop-edit";

  let { recipe, values, blacks, white, problem = null, onchange, onclear, onpatch, sources = {}, croptools, cropediting = null }: {
    recipe: RecipeValues; values: RecipeValues; blacks: number[]; white: number; problem?: ApiError | null;
    croptools?: CropTools; cropediting?: CropEdit | null;
    onchange: (section: string, field: string, value: unknown) => void;
    onclear: (fields: string[]) => void;
    onpatch?: (values: Record<string, unknown>) => void; sources?: Record<string, string>;
  } = $props();
  const sites = ["top left", "top right", "bottom left", "bottom right"];
  const id = $props.id();
  const own = (field: string) => Object.hasOwn(values.develop ?? {}, field);
  const origin = (field: string) => {
    if (["black_level", "white_level"].includes(field) && ["recorded", "static"].includes(String(recipe.develop?.level_policy)))
      return `${own("level_policy") ? "This clip" : sources["level_policy"] ?? "Inherited"}: ${recipe.develop?.level_policy === "recorded" ? "recorded per frame" : "static frame 1"}`;
    return own(field) ? "This clip" : sources[field] ?? (recipe.develop?.[field] != null ? "Inherited override" : "Recording");
  };
  function patch(values: Record<string, unknown>) {
    if (onpatch) onpatch(values);
    else for (const [field, value] of Object.entries(values)) onchange("develop", field, value);
  }
  let recordedLevels = $derived(["recorded", "static"].includes(String(recipe.develop?.level_policy)));
  const error = (field: string) => problem?.field === `develop.${field}` ? problem.message : undefined;
  let black = $derived(recordedLevels ? undefined : recipe.develop?.black_level);
  let shownBlack = $derived(Array.isArray(black) ? black.map(Number) : typeof black === "number" ? Array(4).fill(black) as number[] : blacks);
  let shownWhite = $derived(Number(recordedLevels ? white : recipe.develop?.white_level ?? white));
  let invalid = $state("");
  let matrixProblem = $state("");
  const cameraError = (field: string) => problem?.field === `camera.${field}` ? problem.message : undefined;
  let matrix = $derived.by(() => { const saved = (recipe.camera as Record<string, unknown> | undefined)?.colour_matrix; return Array.isArray(saved) ? saved.join(", ") : ""; });
  let ownMatrix = $derived(Object.hasOwn((values.camera ?? {}) as Record<string, unknown>, "colour_matrix"));
  function chooseMatrix(text: string): void {
    if (!text.trim()) { matrixProblem = ""; onclear(["camera.colour_matrix"]); return; }
    const numbers = text.split(/[\s,]+/).filter(Boolean).map(Number);
    if (numbers.length !== 9 || !numbers.every(Number.isFinite)) { matrixProblem = "Enter nine numbers, row by row."; return; }
    matrixProblem = "";
    onchange("camera", "colour_matrix", numbers);
  }
  let cropMode = $derived(["none", "auto"].includes(String(recipe.develop?.mount_crop ?? "none")) ? String(recipe.develop?.mount_crop ?? "none") : "custom");
  // As shown on the preview when the clip screen can turn it; otherwise in sensor pixels.
  let savedCrop = $derived(croptools ? croptools.saved : parseCrop(recipe.develop?.mount_crop));
  let shape = $derived.by(() => {
    const aspect = cropediting?.aspect;
    if (aspect == null || !croptools) return "free";
    if (aspect === croptools.whole.width / croptools.whole.height) return "recorded";
    return String(ASPECTS.findIndex(item => item.value === aspect));
  });
  function chooseShape(value: string): void {
    if (!croptools) return;
    const choice = value === "recorded" ? "recorded" : value === "free" ? null : ASPECTS[Number(value)]?.value ?? null;
    croptools.shape(choice === "recorded" ? croptools.whole.width / croptools.whole.height : choice);
  }
  function field(name: "left" | "top" | "width" | "height", event: Event): void {
    const value = (event.currentTarget as HTMLInputElement).valueAsNumber;
    if (cropediting && croptools && Number.isFinite(value)) croptools.set({ ...cropediting.box, [name]: value });
  }
  // A rejected RAW setting opens the advanced settings, where its message is shown.
  $effect(() => { if (problem?.field?.startsWith("develop.")) setSectionOpen("raw-advanced", true); });
  // Saved before the picture mode, the setting was a switch: on meant the recorded exposure.
  let normalise = $derived.by(() => {
    const saved = recipe.develop?.mount_normalise_exposure;
    return saved === true ? "recorded" : saved === false || saved == null ? "off" : String(saved);
  });
  // reference application' white/black presets; Static keeps frame 1's recorded levels for every frame.
  const presets = ["1023/64", "4095/256", "16383/1024", "65535/4096", "4095/64", "16383/64", "16383/0"];
  let rate = $derived(String(recipe.develop?.mount_frame_rate ?? "keep"));
  let cameraModel = $derived(recipe.develop?.camera_model_source === "recorded" ? "recorded" : String(recipe.develop?.mount_camera_model ?? ""));
  const rates = ["23.976", "24", "25", "29.97", "30", "48", "50", "59.94", "60"];
  let preset = $derived.by(() => {
    if (!own("level_policy") && !own("black_level") && !own("white_level")) return "inherited";
    if (recipe.develop?.level_policy === "recorded") return "dynamic";
    if (recipe.develop?.level_policy === "static") return "static";
    const savedBlack = recipe.develop?.black_level, savedWhite = recipe.develop?.white_level;
    if (savedBlack == null && savedWhite == null) return "dynamic";
    const single = Array.isArray(savedBlack) ? (new Set(savedBlack).size === 1 ? savedBlack[0] : null) : savedBlack;
    const named = `${Number(savedWhite)}/${Number(single)}`;
    return single != null && presets.includes(named) ? named : "custom";
  });
  const RATE_NAMES: Record<string, string> = { keep: "recorded rate", integer: "nearest whole rate", "drop-frame": "nearest drop-frame rate", average: "average rate", median: "median rate" };
  let mediaSummary = $derived([
    ({ inherited: "Inherited levels", dynamic: "Levels per frame", static: "Static levels", custom: "Custom levels" } as Record<string, string>)[preset]
      ?? `White ${preset.split("/")[0]} / black ${preset.split("/")[1]}`,
    RATE_NAMES[rate] ?? `${rate} fps`,
    recipe.develop?.mount_shading === "baked" ? "vignette baked" : null,
    recipe.develop?.lens_shading_mode === "colour" ? "colour only" : null,
  ].filter(part => part !== null).join(" · "));
  function choosePreset(value: string) {
    if (value === "inherited") { onclear(["level_policy", "black_level", "white_level"]); return; }
    if (value === "dynamic" || value === "static") {
      patch({ level_policy: value === "dynamic" ? "recorded" : "static" }); return;
    }
    const [nextWhite, nextBlack] = value.split("/").map(Number);
    patch({ level_policy: "explicit", black_level: nextBlack, white_level: nextWhite });
  }

  function level(event: Event, field: "white_level" | "black_level", index = 0) {
    const input = event.currentTarget as HTMLInputElement;
    if (input.value === "" || !input.validity.valid || !Number.isFinite(input.valueAsNumber)) {
      invalid = "Enter a RAW code in range. Every black level must be below the white level.";
      return;
    }
    invalid = "";
    const number = input.valueAsNumber;
    patch({ level_policy: "explicit", ...(recordedLevels ? { black_level: shownBlack, white_level: shownWhite } : {}),
      [field]: field === "white_level" ? number : shownBlack.map((value, i) => i === index ? number : value) });
  }
</script>

<SectionCard id="raw-media" title="RAW media" summary={mediaSummary}>
<section aria-label="RAW media settings">
  <p class="muted head wide">For media sent or mounted from now on <InfoTip label="RAW media" text="Newly sent or mounted virtual media uses these settings; existing mounts keep the settings they were made with. Inherited settings come from the folder or application defaults, and the Use inherited buttons restore them." /></p>
  {#if invalid}<p role="alert">{invalid}</p>{/if}
  <div><div class="head"><label for={`${id}-levels`}>White / Black Levels</label><InfoTip label="White / Black Levels" text={`The black and white levels written into mounted DNGs. Recorded per frame follows each frame's levels; Static keeps frame 1's for every frame. Level policy: ${own("level_policy") ? "This clip" : sources.level_policy ?? "Inherited"}. Black: ${origin("black_level")}. White: ${origin("white_level")}.`} /></div>
  <select id={`${id}-levels`} value={preset} onchange={(event) => choosePreset(event.currentTarget.value)}>
    <option value="inherited">Use inherited levels</option>
    <option value="dynamic">Recorded per frame</option>
    <option value="static">Static (frame 1 for every frame)</option>
    {#each presets as name}<option value={name}>White {name.split("/")[0]} / black {name.split("/")[1]}</option>{/each}
    {#if preset === "custom"}<option value="custom" disabled>Custom</option>{/if}
  </select></div>
  <div><div class="head"><label for={`${id}-rate`}>Mounted frame rate</label><InfoTip label="Mounted frame rate" text="A constant rate repeats or drops frames to keep the picture with the sound, which keeps its recorded speed." /></div>
    <select id={`${id}-rate`} value={rate} onchange={(event) => { const value = event.currentTarget.value; onchange("develop", "mount_frame_rate", rates.includes(value) ? Number(value) : value); }}>
      <option value="keep">Recorded (dropped frames filled)</option><option value="integer">Nearest whole rate</option><option value="drop-frame">Nearest drop-frame rate</option><option value="average">Average</option><option value="median">Median (slow motion)</option>
      {#each rates as rate}<option value={String(Number(rate))}>{rate} fps</option>{/each}
      {#if !["keep", "integer", "drop-frame", "average", "median", ...rates].includes(rate)}<option value={rate}>{rate} fps</option>{/if}
    </select></div>
  <fieldset class="wide"><legend>Lens correction</legend>
    <div class="option"><label class="check"><input type="checkbox" role="switch" checked={recipe.develop?.mount_shading === "baked"} onchange={(event) => onchange("develop", "mount_shading", event.currentTarget.checked ? "baked" : "opcode")} />Bake in Vignette Correction</label><InfoTip label="Bake in Vignette Correction" text="Corrects each virtual DNG as Resolve reads it. Off stores gain maps and leaves sensor values untouched. Baking can raise the DNG white level to preserve highlights." /></div>
    <div class="option"><label class="check"><input type="checkbox" role="switch" checked={recipe.develop?.lens_shading_mode === "colour"} onchange={(event) => onchange("develop", "lens_shading_mode", event.currentTarget.checked ? "colour" : "full")} />Reduce to Colour Correction</label><InfoTip label="Reduce to Colour Correction" text="Uses only the colour differences in the lens shading map. The vignette is retained and no correction gain exceeds 1. The Shading correction menu also offers vignette only." /></div>
  </fieldset>
</section>
</SectionCard>
<SectionCard id="raw-advanced" title="Advanced RAW" summary="Levels, colour, shading, exposure, transfer curve, crop">
  <div class="advanced">
  <fieldset>
    <legend class="head">Black levels <span>{origin("black_level")}</span><InfoTip label="Black levels" text="One black level for each Bayer position, read left to right on each row. Recorded values are from frame 1." /></legend>
    <div class="sites">
      {#each sites as site, i}
        <label>{site}<input aria-label={`Black ${site}`} type="number" min="0" max={Math.min(65535, shownWhite - 0.000001)} step="any" value={shownBlack[i]} onchange={(event) => level(event, "black_level", i)} /></label>
      {/each}
    </div>
    <p class="muted">Recorded: {blacks.map(value => Number(value.toFixed(2))).join(", ")}</p>
    {#if error("black_level")}<p role="alert">{error("black_level")}</p>{/if}
    <button class="secondary" disabled={!own("black_level")} onclick={() => onclear(own("white_level") ? ["black_level"] : ["black_level", "level_policy"])}>Use inherited black levels</button>
  </fieldset>
  <fieldset>
    <legend class="head">White level <span>{origin("white_level")}</span><InfoTip label="White level" text="The RAW code at which the sensor clips. Every black level must be below it. The recorded value is from frame 1." /></legend>
    <label>White level<input aria-label="White level" type="number" min={Math.max(1, ...shownBlack.map(value => value + 0.000001))} max="65535" step="any" value={shownWhite} onchange={(event) => level(event, "white_level")} /></label>
    <p class="muted">Recorded: {white}</p>
    {#if error("white_level")}<p role="alert">{error("white_level")}</p>{/if}
    <button class="secondary" disabled={!own("white_level")} onclick={() => onclear(own("black_level") ? ["white_level"] : ["white_level", "level_policy"])}>Use inherited white level</button>
  </fieldset>
  <fieldset>
    <legend class="head">Colour filter <span>{own("cfa_layout") || own("cfa_pattern") ? "This clip" : sources.cfa_layout ?? sources.cfa_pattern ?? "Recording"}</span><InfoTip label="Colour filter" text="How the sensor's colour filter is read, for recordings whose metadata is wrong. Quad Bayer remosaics 2×2 colour cells to the Bayer pattern; Bayer reads the values as recorded. A wrong Bayer order swaps colours or turns the picture magenta and green." /></legend>
    <label>Layout<select value={String(recipe.develop?.cfa_layout ?? "auto")} onchange={(event) => onchange("develop", "cfa_layout", event.currentTarget.value)}>
      <option value="auto">Automatic (from the recording)</option><option value="bayer">Bayer, as recorded</option><option value="quad">Quad Bayer (remosaic)</option>
    </select></label>
    <label>Bayer order<select value={String(recipe.develop?.cfa_pattern ?? "recorded")} onchange={(event) => onchange("develop", "cfa_pattern", event.currentTarget.value)}>
      <option value="recorded">As recorded</option><option value="rggb">RGGB</option><option value="bggr">BGGR</option><option value="grbg">GRBG</option><option value="gbrg">GBRG</option>
    </select></label>
    {#if error("cfa_layout")}<p role="alert">{error("cfa_layout")}</p>{/if}
    {#if error("cfa_pattern")}<p role="alert">{error("cfa_pattern")}</p>{/if}
    <button class="secondary" disabled={!own("cfa_layout") && !own("cfa_pattern")} onclick={() => onclear(["cfa_layout", "cfa_pattern"])}>Use inherited colour filter</button>
  </fieldset>
  <fieldset>
    <legend class="head">Colour matrix <span>{ownMatrix ? "This clip" : matrix ? "Inherited" : "Recording"}</span><InfoTip label="Colour matrix" text="Nine numbers, row by row, mapping XYZ to the camera's colours (DNG ColorMatrix). They replace the recording's matrices for both illuminants in the preview, exports and mounted DNGs. Leave empty to use the recording's." /></legend>
    <label>XYZ to camera<input type="text" spellcheck="false" placeholder="Recording's matrices" value={matrix} onchange={(event) => chooseMatrix(event.currentTarget.value)} /></label>
    {#if matrixProblem}<p role="alert">{matrixProblem}</p>{/if}
    {#if cameraError("colour_matrix")}<p role="alert">{cameraError("colour_matrix")}</p>{/if}
    <button class="secondary" disabled={!ownMatrix} onclick={() => { matrixProblem = ""; onclear(["camera.colour_matrix"]); }}>Use inherited matrix</button>
  </fieldset>
  <fieldset>
    <legend>Lens shading</legend>
    <div class="head"><label for={`${id}-shading`}>Shading correction</label><InfoTip label="Shading correction" text={`${own("lens_shading_mode") ? "This clip" : "Inherited"}. Colour only preserves the vignette and uses no gain above 1. Vignette only corrects the falloff and leaves colours as recorded.`} /></div>
    <select id={`${id}-shading`} value={String(recipe.develop?.lens_shading_mode ?? "full")} onchange={(event) => onchange("develop", "lens_shading_mode", event.currentTarget.value)}>
      <option value="full">Vignette and colour</option><option value="colour">Colour only</option><option value="vignette">Vignette only</option>
    </select>
    <Slider label="Shading strength" min={0} max={1} value={Number(recipe.develop?.lens_shading ?? 1)} error={error("lens_shading")} onchange={(value) => onchange("develop", "lens_shading", value)}
      info={`${own("lens_shading") ? "This clip" : "Inherited"}. 0 keeps the recorded shading; 1 applies the full correction.`} />
    {#if error("lens_shading_mode")}<p role="alert">{error("lens_shading_mode")}</p>{/if}
    <button class="secondary" disabled={!own("lens_shading") && !own("lens_shading_mode")} onclick={() => onclear(["lens_shading", "lens_shading_mode"])}>Use inherited shading</button>
  </fieldset>
  <fieldset class="wide">
    <legend>Virtual CinemaDNG <span>{own("mount_shading") ? "This clip" : "Inherited"}</span></legend>
    <div class="head"><label for={`${id}-normalise`}>Normalise exposure</label><InfoTip label="Normalise exposure" text="Recorded evens out exposure changes from each frame's ISO and shutter as the editor reads it, with nothing to wait for; changes in the scene's own brightness are kept. Measured from the picture measures every frame's brightness once before mounting (about a minute for ten minutes of 4K), then evens each frame out towards the average over the smoothing window: auto exposure flicker and the steps of a holy-grail timelapse go, slow changes stay. Up to two stops of headroom keep brightened frames from clipping." /></div>
    <select id={`${id}-normalise`} value={normalise} onchange={(event) => onchange("develop", "mount_normalise_exposure", event.currentTarget.value)}>
      <option value="off">Off</option><option value="recorded">To the first frame's recorded exposure</option><option value="picture">Measured from the picture</option>
    </select>
    {#if normalise === "picture"}
      <label>Smoothing window (seconds)<input type="number" min="0.1" max="60" step="0.1" value={Number(recipe.develop?.mount_normalise_seconds ?? 1)} onchange={(event) => { const value = Number(event.currentTarget.value); if (event.currentTarget.value !== "" && value >= 0.1 && value <= 60) onchange("develop", "mount_normalise_seconds", value); }} /></label>
    {/if}
    {#if error("mount_normalise_seconds")}<p role="alert">{error("mount_normalise_seconds")}</p>{/if}
    <div class="head"><label for={`${id}-transfer`}>Transfer curve in mounted DNGs</label><InfoTip label="Transfer curve" text="Log serves the values as log codes with dithering, as a log CinemaDNG export; codes of 8 bits or fewer take one byte each, so editors read half as much. Every DNG reader turns them back into linear values." /></div>
    <select id={`${id}-transfer`} value={String(recipe.develop?.mount_transfer ?? "linear")} onchange={(event) => onchange("develop", "mount_transfer", event.currentTarget.value)}>
      <option value="linear">Linear (sensor values)</option><option value="log">Log (smaller values, with a linearisation table)</option>
    </select>
    {#if (recipe.develop?.mount_transfer ?? "linear") === "log"}
      <label>Bits removed<select value={String(recipe.develop?.mount_reduce_bits ?? "auto")} onchange={(event) => { const value = event.currentTarget.value; onchange("develop", "mount_reduce_bits", value === "auto" ? value : Number(value)); }}>
        <option value="auto">Auto (the sensor's bits less 2)</option><option value="2">2 bits</option><option value="4">4 bits</option><option value="6">6 bits</option><option value="8">8 bits</option>
      </select></label>
    {/if}
    <div class="option"><label class="check"><input type="checkbox" role="switch" disabled={(recipe.develop?.mount_transfer ?? "linear") === "log"} checked={Boolean(recipe.develop?.mount_scale)} onchange={(event) => onchange("develop", "mount_scale", event.currentTarget.checked)} />Scale values to 16 bits</label><InfoTip label="Scale values to 16 bits" text="Multiplies the served values so the white level is 65535, with the black levels in proportion. The picture is unchanged; some editors handle a full 16-bit range better than a 10- or 12-bit one. Linear mounts only." /></div>
    {#if error("mount_scale")}<p role="alert">{error("mount_scale")}</p>{/if}
    {#if error("mount_crop")}<p role="alert">{error("mount_crop")}</p>{/if}
    {#if error("mount_reduce_bits")}<p role="alert">{error("mount_reduce_bits")}</p>{/if}
    <div class="head"><label for={`${id}-crop`}>Mounted DNG crop</label><InfoTip label="Mounted DNG crop" text="Automatic removes the black strip some 60 fps recordings leave on one edge, found in the first, middle and last frames when mounting. Custom keeps a rectangle you place on the preview; it is saved in sensor pixels and its edges align to 2 × the proxy factor. Cropping keeps sensor calibration and audio timing. Stabilised timeline clips need uncropped media." /></div>
    <select id={`${id}-crop`} value={cropediting ? "custom" : cropMode} onchange={(event) => {
      const value = event.currentTarget.value;
      if (value === "custom") croptools?.start();
      else { croptools?.cancel(); onchange("develop", "mount_crop", value); }
    }}>
      <option value="none">None</option><option value="auto">Remove a black edge (automatic)</option><option value="custom">Custom rectangle</option>
    </select>
    {#if cropediting && croptools}
      <div class="crop-tools" role="group" aria-label="Place the mounted DNG crop">
        <div class="head"><label for={`${id}-shape`}>Shape</label><InfoTip label="Placing the crop" text={`Drag inside the rectangle on the preview to move it, or drag an edge or corner to resize it. Every edge snaps to ${croptools.block} sensor pixels, so the colour pattern and proxies stay aligned. Sizes are of the picture as shown, before any proxy.`} /></div>
        <select id={`${id}-shape`} value={shape} onchange={(event) => chooseShape(event.currentTarget.value)}>
          {#each ASPECTS as item, i}<option value={item.value === null ? "free" : item.value === "recorded" ? "recorded" : String(i)}>{item.label}</option>{/each}
        </select>
        <div class="sizes">
          <label>Width<input aria-label="Crop width" type="number" min={croptools.block} max={croptools.whole.width} step={croptools.block} value={Math.round(cropediting.box.width)} onchange={(event) => field("width", event)} /></label>
          <label>Height<input aria-label="Crop height" type="number" min={croptools.block} max={croptools.whole.height} step={croptools.block} value={Math.round(cropediting.box.height)} onchange={(event) => field("height", event)} /></label>
          <label>From left<input aria-label="Crop left" type="number" min="0" max={croptools.whole.width} step={croptools.block} value={Math.round(cropediting.box.left)} onchange={(event) => field("left", event)} /></label>
          <label>From top<input aria-label="Crop top" type="number" min="0" max={croptools.whole.height} step={croptools.block} value={Math.round(cropediting.box.top)} onchange={(event) => field("top", event)} /></label>
        </div>
        <div class="actions">
          <button class="secondary" onclick={() => croptools.set({ ...cropediting.box, left: (croptools.whole.width - cropediting.box.width) / 2, top: (croptools.whole.height - cropediting.box.height) / 2 })}>Centre</button>
          <button class="secondary" onclick={() => croptools.set({ left: 0, top: 0, ...croptools.whole })}>Whole frame</button>
          <button onclick={() => croptools.apply()}>Apply crop</button>
          <button class="secondary" onclick={() => croptools.cancel()}>Cancel</button>
        </div>
      </div>
    {:else if cropMode === "custom" && savedCrop}
      <div class="option"><span class="muted">{savedCrop.width} × {savedCrop.height}{#if savedCrop.left || savedCrop.top}, {savedCrop.left} from the left and {savedCrop.top} from the top{:else}, from the top-left{/if}</span>
        {#if croptools}<button class="secondary" onclick={() => croptools.start()}>Edit on preview</button>{/if}</div>
    {/if}
    <label>Camera named in mounted DNGs<select value={cameraModel} onchange={(event) => { const value = event.currentTarget.value; if (value === "recorded") patch({ camera_model_source: "recorded" }); else if (value) patch({ camera_model_source: "override", mount_camera_model: value }); else onclear(["camera_model_source", "mount_camera_model"]); }}>
      <option value="">Use inherited camera (recording phone when unset)</option><option value="recorded">Recording camera</option><option value="Panasonic Varicam RAW">Panasonic Varicam RAW</option><option value="Blackmagic Pocket Cinema Camera 4K">Blackmagic Pocket Cinema Camera 4K</option>
      {#if cameraModel && cameraModel !== "recorded" && !["Panasonic Varicam RAW", "Blackmagic Pocket Cinema Camera 4K"].includes(cameraModel)}<option value={cameraModel}>{cameraModel}</option>{/if}
    </select></label>
    <div class="option"><label class="check"><input type="checkbox" role="switch" checked={Boolean(recipe.develop?.mount_unique_model)} onchange={(event) => onchange("develop", "mount_unique_model", event.currentTarget.checked)} />Add the clip's name to the camera model</label>
    <InfoTip label="Camera model" text="Another camera name can give other defaults in Resolve's RAW tab; a unique name keeps each clip's RAW settings apart. Neither makes Resolve offer Camera Gyro for CinemaDNG." /></div>
    {#if error("mount_shading")}<p role="alert">{error("mount_shading")}</p>{/if}
    {#if error("mount_proxy")}<p role="alert">{error("mount_proxy")}</p>{/if}
    {#if error("mount_frame_rate")}<p role="alert">{error("mount_frame_rate")}</p>{/if}
    <button class="secondary" disabled={!own("mount_shading")} onclick={() => onclear(["mount_shading"])}>Use inherited mount shading</button>
    <button class="secondary" disabled={!["mount_proxy", "mount_first_frame", "mount_scale", "mount_frame_rate", "mount_normalise_exposure", "mount_normalise_seconds", "mount_transfer", "mount_reduce_bits", "mount_crop", "camera_model_source", "mount_camera_model", "mount_unique_model"].some(own)} onclick={() => onclear(["mount_proxy", "mount_first_frame", "mount_scale", "mount_frame_rate", "mount_normalise_exposure", "mount_normalise_seconds", "mount_transfer", "mount_reduce_bits", "mount_crop", "camera_model_source", "mount_camera_model", "mount_unique_model"])}>Use inherited mount settings</button>
  </fieldset>
  </div>
</SectionCard>

<style>
  section { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; font-size: 12px; }
  .advanced { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 12px; }
  section > p, .wide { grid-column: 1 / -1; }
  .option { display: flex; align-items: center; gap: 4px; }
  .head { display: flex; align-items: center; gap: 2px; min-width: 0; }
  legend.head { gap: 0; }
  .head + select { margin: 4px 0 8px; }
  .crop-tools { display: grid; gap: 4px; padding: 8px; margin-bottom: 8px; border: 1px solid var(--accent); border-radius: 6px; }
  .sizes { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 8px; }
  .actions { display: flex; flex-wrap: wrap; gap: 6px; }
  fieldset { border: 1px solid var(--line); border-radius: 6px; padding: 10px; min-width: 0; }
  legend { color: color-mix(in srgb, var(--text) 80%, var(--muted)); font-weight: 600; } legend span { margin-left: 6px; color: var(--muted); font-weight: 400; font-size: 10px; }
  .sites { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  label { display: grid; gap: 6px; margin: 4px 0 8px; }
  label.check { display: flex; align-items: center; gap: 8px; }
  input:not([type="checkbox"]), select { min-width: 0; width: 100%; box-sizing: border-box; }
  input:invalid { border-color: var(--danger, #ef9a91); }
  .sites label { text-transform: capitalize; }
  .muted { color: var(--muted); line-height: 1.5; margin: 6px 0; }
  button { margin-top: 6px; font-size: 11px; } button:disabled { opacity: .5; cursor: not-allowed; }
  @container (max-width: 380px) { section, .advanced { grid-template-columns: minmax(0, 1fr); } }
  .secondary { background: transparent; border: 1px solid var(--line); color: var(--text); }
</style>
