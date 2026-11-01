<script lang="ts">
  import { onMount, onDestroy, untrack } from "svelte";
  import type { Snippet } from "svelte";
  import { get } from "svelte/store";
  import { settings } from "../lib/settings";
  import { api, developApi, type ApiError, type RecipeValues, type RecipeLayer } from "../lib/api";
  import { clips, clipViews, openedClips, setClipView, libraryChanges, thumbnailUrl, refreshThumbnail, type ClipSummary, type ClipDetails } from "../lib/library";
  import { clipDetails } from "../lib/clip-details";
  import { displaySize } from "../lib/display-size";
  import { takePreview, previewRecipe } from "../lib/preview-opening";
  import { PreviewClient, type PreviewState } from "../lib/preview";
  import { timeSeconds, secondsSyntax } from "../lib/trim";
  import { trimSummary } from "../lib/suggested-trims";
  import type { Painter } from "../components/ScrubCanvas.svelte";
  import PreviewCanvas from "../components/PreviewCanvas.svelte";
  import PreviewCache from "../components/PreviewCache.svelte";
  import NativeViewport from "../components/NativeViewport.svelte";
  import Transport from "../components/Transport.svelte";
  import TrimBar from "../components/TrimBar.svelte";
  import ChartOverlay from "../components/ChartOverlay.svelte";
  import OverlaySelector from "../components/OverlaySelector.svelte";
  import ClippingOverlay from "../components/ClippingOverlay.svelte";
  import { tick } from "svelte";
  import type { OverlayModel } from "../lib/preview-overlay";
  import Viewport from "../components/Viewport.svelte";
  import DevelopSelection from "../components/DevelopSelection.svelte";
  import DevelopPanel from "../components/DevelopPanel.svelte";
  import CropGuide from "../components/CropGuide.svelte";
  import Warp from "../components/Warp.svelte";
  import { stabilisedView } from "../lib/warp";
  import CropEditor from "../components/CropEditor.svelte";
  import { cropEdit } from "../lib/crop-edit";
  import { PROXY_FACTORS, formatCrop, fitAspect, parseCrop, shownSize, snap, toRecorded, toShown, type Box } from "../lib/dng-crop";
  import type { CropOutline } from "../lib/native-preview";
  import PreviewDivider, { savedPreviewHeight } from "../components/PreviewDivider.svelte";

  // The Library's right pane: the picture, with the clip's settings rendered by `inspector`. `ondock` lets
  // the picture move to where the Library's clip list is (`docked`), the settings beside it at the top.
  let { id, inspector, docked = false, ondock, onflush, onplayback = () => {} }: {
    id: string; onplayback?: (value: boolean) => void;
    inspector: Snippet<[Snippet, string]>; docked?: boolean; ondock?: () => void;
    onflush?: (flush: (() => Promise<boolean>) | null) => void;
  } = $props();
  onDestroy(() => { onplayback(false); if ($cropEdit?.id === id) cropEdit.set(null); });
  let recipe = $state<RecipeValues | null>(null);
  let section: HTMLElement | undefined = $state();
  let previewHeight = $state<number | null>(savedPreviewHeight());
  let layer = $state<RecipeLayer>({ version: "", values: {} });
  let sources = $state<Record<string, string>>({});
  let inherited = $state<RecipeValues>({});
  let raw = $state<{ blacks: number[]; white: number } | null>(null);
  let colourNote = $state<string | null>(null);
  let detectedTransfer = $state<string | null>(null);
  let transport: { pause: () => void } | undefined = $state();
  let orientationHint = $state<import("../lib/api").Schemas["OrientationHint"] | null>(null);
  let geometry = $state<ClipSummary | null>(null);

  let missing = $state(false);
  let problem = $state<ApiError | null>(null);
  let message = $state("");
  let savingText = $state("");
  let client = $state<PreviewClient | null>(null);
  let preview = $state<PreviewState>({ frame: null, kind: "opening", ms: 0, header: null, problem: null });
  let display = $derived.by(() => {
    const width = preview.header?.frame_width, height = preview.header?.frame_height;
    if (width && height) return { width, height };
    return displaySize(geometry, typeof recipe?.develop?.rotation === "number" ? recipe.develop.rotation : geometry?.display_rotation ?? 0);
  });
  let imageWidth = $derived(display.width);
  let imageHeight = $derived(display.height);
  let index = $state(0);
  let clippingMode = $state<"off" | "any" | "all">("off");
  let overlayModel = $state<OverlayModel | null>(null);
  let count = $state(1);
  let rate = $state(24);
  let resolution = $derived($clipViews[id]?.resolution ?? "adaptive");
  let supportsSource = $state(false);
  let paintAfter: Painter | null = null;
  let active = true;
  let peaks = $state<number[]>([]); // the clip's sound, drawn on the trim bar
  let title = $derived([...($clips ?? []), ...$openedClips].find((clip) => clip.id === id)?.name ?? null); // for the desktop's media controls
  let ready = $state(false);
  let timelineReady = $state(false);
  let timer: ReturnType<typeof setTimeout> | null = null;
  let revision = 0;
  let colourRevision = 0;
  let saving = $state(false);
  let resetRevision: number | null = null;
  let playing = $state(false);
  let clippingActive = $derived(clippingMode !== "off" && !playing && geometry?.kind === "raw");
  // The stabilised frame is outlined on the (unstabilised) preview.
  let cropGuide = $derived(recipe && geometry?.gyro && recipe.motion?.stabilise !== "off" ? Number(recipe.motion?.crop ?? 1.1) : 0);
  // Planned as a timeline send plans the clip, the kept frame moves with the correction. Until then, and
  // when the clip cannot be planned, the guide keeps the fixed crop. Only these settings change the plan.
  // Image-assisted refinement takes most of a clip's first plan, so a quick plan without it comes first.
  let keptKey = $derived(cropGuide > 1 && recipe && geometry?.kind === "raw" ? JSON.stringify([id, recipe.motion, recipe.camera, recipe.develop?.rotation, recipe.develop?.crop, recipe.develop?.mount_crop]) : "");
  let kept = $state<{ key: string; corners: number[][]; reason: string | null; refining: boolean } | null>(null);
  let keptNow = $derived(kept?.key === keptKey ? kept : null);
  // Why this clip cannot be planned: with only a still outline, Stabilise would seem to do nothing.
  let notPlanned = $derived(keptNow && !keptNow.corners.length ? keptNow.reason : null);
  $effect(() => {
    const key = keptKey;
    if (!key || untrack(() => kept?.key === key && !kept.refining)) return;
    const asked = untrack(() => recipe!), clip = untrack(() => id);
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      for (const quick of asked.motion?.optical_correction === false ? [false] : [true, false]) {
        const result = await api.keptFrame(clip, asked, quick, controller.signal);
        if (controller.signal.aborted) return;
        if (quick && !(result.ok && result.data.corners?.length)) continue;  // the full plan says why
        kept = result.ok ? { key, corners: result.data.corners ?? [], reason: result.data.reason ?? null, refining: quick } : { key, corners: [], reason: result.message, refining: false };
      }
    }, 500);
    return () => { clearTimeout(timer); controller.abort(); };
  });
  // The mounted DNG crop being placed on the preview, which then shows the whole recorded frame.
  let editing = $derived($cropEdit?.id === id ? $cropEdit : null);
  let editingCrop = $derived(editing !== null);
  // Shown stabilised, each frame's kept frame fills the picture (not while a DNG crop is being placed).
  let warping = $derived($stabilisedView && !editing && keptNow?.corners.length ? keptNow.corners : null);
  let rotation = $derived(typeof recipe?.develop?.rotation === "number" ? recipe.develop.rotation : geometry?.display_rotation ?? 0);
  let recorded = $derived({ width: geometry?.width ?? 1920, height: geometry?.height ?? 1080 });
  let whole = $derived(shownSize(recorded.width, recorded.height, rotation));
  let block = $derived(2 * (PROXY_FACTORS[String(recipe?.develop?.mount_proxy ?? "full")] ?? 1));
  let outline = $derived.by((): CropOutline | null => {
    if (editing) {
      const { left, top, width, height } = editing.box;
      return { x: left / whole.width, y: top / whole.height, width: Math.min(1, width / whole.width), height: Math.min(1, height / whole.height), handles: true };
    }
    if (cropGuide <= 1) return null;
    const box = { x: (1 - 1 / cropGuide) / 2, y: (1 - 1 / cropGuide) / 2, width: 1 / cropGuide, height: 1 / cropGuide };
    return keptNow?.corners.length ? { ...box, corners: keptNow.corners, stabilised: !!warping } : box;
  });
  // Snapped in the recorded frame, where the engine checks the Bayer phase and proxy alignment.
  function placed(box: Box): Box {
    return toShown(snap(toRecorded(box, rotation, recorded.width, recorded.height), block, recorded.width, recorded.height), rotation, recorded.width, recorded.height);
  }
  const croptools = {
    get whole() { return whole; },
    get saved() { const saved = parseCrop(recipe?.develop?.mount_crop); return saved ? toShown(saved, rotation, recorded.width, recorded.height) : null; },
    get block() { return block; },
    start() {
      const saved = parseCrop(recipe?.develop?.mount_crop);
      const box = saved ? toShown(saved, rotation, recorded.width, recorded.height) : { left: 0, top: 0, ...whole };
      transport?.pause();
      cropEdit.set({ id, box: placed(box), aspect: null });
    },
    set(box: Box) { if (editing) cropEdit.set({ ...editing, box: placed(box) }); },
    shape(aspect: number | null) {
      if (!editing) return;
      cropEdit.set({ ...editing, aspect, box: placed(aspect ? fitAspect(aspect === whole.width / whole.height ? { left: 0, top: 0, ...whole } : editing.box, aspect) : editing.box) });
    },
    apply() {
      if (!editing) return;
      setValue("develop", "mount_crop", formatCrop(toRecorded(editing.box, rotation, recorded.width, recorded.height)));
      cropEdit.set(null);
    },
    cancel() { if (editing) cropEdit.set(null); },
  };
  $effect(() => { void rotation; untrack(() => croptools.cancel()); });
  $effect(() => { void block; untrack(() => { if (editing) croptools.set(editing.box); }); });
  function shown(saved: RecipeValues): RecipeValues {
    return previewRecipe(editingCrop ? { ...saved, develop: { ...saved.develop, mount_crop: "none" } } : saved);
  }
  $effect(() => {
    void editingCrop;
    untrack(() => { if (ready && recipe) client?.request("recipe", { recipe: shown(recipe), index, fast: true }); });
  });
  let preparing = false;
  let frames = 0;
  let lastPlaybackIndex = -1;
  let rateTimer: ReturnType<typeof setInterval> | null = null;
  let started = 0;
  let reached = $state(0);
  // A draft that is replaced quickly is not announced, so the status line does not blink on every seek.
  let refining = $state(false);
  let drafting = $derived(preview.kind === "draft" && !playing && !preview.header?.source);
  $effect(() => {
    if (!drafting) { refining = false; return; }
    const shown = setTimeout(() => { refining = true; }, 400);
    return () => clearTimeout(shown);
  });
  let trimStart = $derived(Math.round(timeSeconds(recipe?.trim?.start, rate) * rate));
  let trimEnd = $derived(recipe?.trim?.duration ? trimStart + Math.round(timeSeconds(recipe.trim.duration, rate) * rate) : count);

  function clipMetadata(clip: ClipSummary) {
    const video = (clip as ClipDetails).metadata?.Video as Record<string, unknown> | undefined;
    colourNote = typeof video?.colour_note === "string" ? video.colour_note : null;
    detectedTransfer = typeof video?.transfer_id === "string" ? video.transfer_id : null;
    orientationHint = clip.orientation_hint ?? null;
    if (clip.kind === "raw" && clip.black_levels && clip.white_level != null) raw = { blacks: clip.black_levels, white: clip.white_level };
    geometry = clip;
    if (!ready && clip.fps && clip.fps > 0 && clip.duration_s && clip.duration_s > 0) {
      rate = clip.fps;
      count = Math.max(1, Math.round(clip.duration_s * rate));
      timelineReady = true;
    }
  }

  function fail(error: ApiError) {
    if (!active) return;
    if (error.code === "clip-missing") {
      missing = true; client?.close();
      if (timer) clearTimeout(timer);
    } else problem = error;
  }
  async function load() {
    trimNote = "";
    const result = await developApi.recipe(id);
    if (!active || missing) return;
    if (!result.ok) { fail(result); return; }
    layer = result.data.layer;
    recipe = result.data.resolved;
    inherited = result.data.inherited ?? {};
    sources = result.data.inherited_sources ?? Object.fromEntries(Object.entries(result.data.sources ?? {}).filter(([, source]) => source !== "This clip"));
    problem = null;
    if (ready) client?.request("recipe", { recipe: shown(recipe), index, fast: true });
  }
  function queueSave() {
    revision += 1;
    if (timer) clearTimeout(timer);
    savingText = "Unsaved changes";
    timer = setTimeout(() => { timer = null; void save(); }, 500);
  }
  async function save() {
    if (!active || missing || saving) return;
    saving = true;
    const sentRevision = revision;
    const sent = $state.snapshot(layer);
    savingText = "Saving...";
    const result = await developApi.save(id, sent);
    saving = false;
    if (!active) {
      if (result.ok && sentRevision !== revision && !missing) {
        void developApi.save(id, { version: result.data.version, values: layer.values });
      }
      return;
    }
    if (missing) return;
    if (!result.ok) {
      savingText = "Not saved";
      if (result.code === "recipe-changed") {
        if (timer) clearTimeout(timer);
        timer = null;
        revision += 1;
        await load();
        if (active && !missing) message = "This clip's settings were changed elsewhere; reloaded";
      } else {
        fail(result);
        if (sentRevision !== revision && timer === null) void save();
      }
      return;
    }
    layer = { version: result.data.version, values: sentRevision === revision ? result.data.values : layer.values };
    if (colourRevision && sentRevision >= colourRevision) { colourRevision = 0; refreshThumbnail(id); }
    savingText = sentRevision === revision ? "Saved" : "Unsaved changes";
    if (resetRevision === sentRevision && sentRevision === revision) {
      resetRevision = null;
      await load();
    }
    if (sentRevision !== revision && timer === null) void save();
  }
  function setValue(section: string, field: string, value: unknown) {
    setValues(section, { [field]: value });
  }
  function setValues(section: string, values: Record<string, unknown>) {
    if (!recipe || missing) return;
    if (section === "output" && "frame_rate" in values) transport?.pause();
    const patch = section === "" ? values : { [section]: { ...(recipe[section] as Record<string, unknown> ?? {}), ...values } };
    recipe = { ...recipe, ...patch };
    layer = { ...layer, values: { ...layer.values, ...(section === "" ? patch : { [section]: { ...(layer.values[section] as Record<string, unknown> ?? {}), ...values } }) } };
    problem = null; message = "";
    queueSave();
    if (section === "develop" && ["input_encoding", "input_gamut", "input_transfer", "input_ei", "native_as"].some(key => key in values)) colourRevision = revision;
    if (ready) client?.request("recipe", { recipe: shown(recipe), index, fast: true });
  }
  // Fields are develop's unless named with their section, as "camera.colour_matrix".
  function clearRaw(fields: string[]) {
    if (!recipe || missing) return;
    const values = { ...layer.values };
    const next = { ...recipe };
    for (const name of fields) {
      const [section, field] = name.includes(".") ? name.split(".", 2) as [string, string] : ["develop", name];
      const resolved = { ...(next[section] as Record<string, unknown> ?? {}) };
      const overrides = { ...(values[section] as Record<string, unknown> ?? {}) };
      const lower = inherited[section] as Record<string, unknown> | undefined;
      delete overrides[field];
      if (lower?.[field] != null) resolved[field] = lower[field];
      else delete resolved[field];
      next[section] = resolved;
      if (Object.keys(overrides).length) values[section] = overrides;
      else delete values[section];
    }
    recipe = next;
    layer = { ...layer, values };
    problem = null; message = ""; queueSave();
    if (ready) client?.request("recipe", { recipe: shown(recipe), index, fast: true });
  }
  function seek(value: number) {
    index = value;
    if (ready && recipe) client?.request("recipe", { recipe: shown(recipe), index, seek: !playing && !preparing, playback: playing || preparing, ahead: playing || preparing ? 3 : 1, fast: true });
  }
  function scrub(value: number) {
    index = value;
    if (ready && recipe) client?.scrub(index, { recipe: shown(recipe) });
  }
  function measureRate() {
    reached = frames / Math.max((performance.now() - started) / 1000, 0.001);
  }
  function preparePlayback(value: number | null) {
    preparing = value !== null;
    onplayback(preparing || playing);
    if (value !== null) { index = value; client?.present(value); }
    if (ready && recipe) client?.request("recipe", { recipe: shown(recipe), index, playback: preparing, ahead: preparing ? 3 : 1, fast: true });
  }
  function playbackChanged(value: boolean) {
    preparing = false;
    playing = value;
    onplayback(value);
    if (rateTimer) clearInterval(rateTimer);
    rateTimer = null;
    if (value) {
      started = performance.now(); frames = 0; lastPlaybackIndex = -1; reached = 0;
      rateTimer = setInterval(measureRate, 500);
    } else {
      if (ready) client?.request("refine", { index });
    }
  }
  let trimNote = $state("");
  let suggesting = $state(false);
  // Marks from the gyro log: past the jolt of pressing record, before the jolt of pressing stop, and
  // after focus settles. Applied because the user asked; the reasons say what moved.
  async function suggestTrim() {
    if (suggesting) return;
    const asked = id;
    suggesting = true; trimNote = "";
    try {
      const report = await api.motion(asked);
      if (asked !== id) return;
      if (!report.ok) { trimNote = report.message; return; }
      const trim = report.data.trim;
      if (trim?.reasons?.length) {
        const first = Math.max(0, Math.min(count - 1, Math.round(trim.start_s * rate)));
        mark(first, Math.max(first + 1, Math.min(count, Math.round(trim.end_s * rate))));
      }
      trimNote = trimSummary(trim, report.data.notes);
    } finally { suggesting = false; }
  }
  function mark(start: number, end: number) {
    if (!recipe) return;
    const trim = { start: secondsSyntax(start / rate), duration: secondsSyntax((end - start) / rate) };
    recipe = { ...recipe, trim };
    layer = { ...layer, values: { ...layer.values, trim } };
    queueSave();
  }
  async function reset() {
    layer = { ...layer, values: {} };
    // Resolve the engine defaults by saving the empty layer, without discarding folder settings.
    revision += 1;
    colourRevision = revision;
    if (timer) clearTimeout(timer);
    timer = null;
    resetRevision = revision;
    if (!saving) await save();
  }

  async function flush(): Promise<boolean> {
    if (timer) clearTimeout(timer);
    timer = null;
    while (saving) await new Promise(resolve => setTimeout(resolve, 25));
    if (savingText === "Unsaved changes") await save();
    while (saving) await new Promise(resolve => setTimeout(resolve, 25));
    return !missing && savingText !== "Not saved" && savingText !== "Unsaved changes";
  }

  onMount(() => {
    active = true;
    onflush?.(flush);
    void api.waveform(id).then((result) => { if (active && result.ok && Array.isArray(result.data.peaks)) peaks = result.data.peaks; });
    const known = get(clips)?.find((clip) => clip.id === id);
    if (known) clipMetadata(known);
    let firstLibrary = true;
    const unsubscribeLibrary = libraryChanges.subscribe((change) => {
      if (firstLibrary) { firstLibrary = false; return; }
      if (change?.removed.includes(id)) fail({ ok: false, code: "clip-missing", message: "" });
    });
    let unsubscribePreview = () => {};
    void (async () => {
      // The pane shows, edits, mounts and sends the saved settings and marks, so its picture uses them too.
      const opening = takePreview(id);
      client = opening.client;
      client.warmFrom = () => index < trimStart || index >= trimEnd - 1 ? trimStart : index;
      void clipDetails(id, known ?? null).then((details) => {
        if (!active || missing) return;
        if (details.ok) clipMetadata(details.data);
        else fail(details);
      });
      client.onpaint = (images, header) => { paintAfter?.(images[0], header); };
      client.onopened = (header) => {
        ready = true; count = header.frame_count!; rate = header.rate!;
        supportsSource = header.supports_source === true;
        client!.sourceResolution = supportsSource && resolution === "source";
        index = Math.max(0, Math.min(count - 1, index));
        message = "";
        if (recipe) client?.request("recipe", { recipe: shown(recipe), index, playback: playing || preparing, ahead: playing || preparing ? 3 : 1, fast: true });
      };
      client.onframe = (header) => {
        if (header.frame_count !== undefined && header.rate !== undefined && (header.frame_count !== count || header.rate !== rate)) {
          count = header.frame_count; rate = header.rate;
          index = Math.max(0, Math.min(count - 1, header.index ?? index));
        }
        if (playing && header.kind === "draft" && typeof header.index === "number" && header.index !== lastPlaybackIndex) {
          lastPlaybackIndex = header.index; frames += 1;
          measureRate();
        }
      };
      unsubscribePreview = client.state.subscribe((state) => { preview = state; if (state.problem?.code === "clip-missing") fail({ ok: false, ...state.problem }); });
      if (client.opened) client.onopened(client.opened);
      const result = await opening.recipe;
      if (!active || missing) return;
      if (!result.ok) { fail(result); client?.close(); return; }
      layer = result.data.layer; recipe = result.data.resolved; inherited = result.data.inherited ?? {};
      if (ready) client?.request("recipe", { recipe: shown(recipe), index, playback: playing || preparing, ahead: playing || preparing ? 3 : 1, fast: true });
    })();
    return () => {
      onflush?.(null);
      if (timer && !missing && !saving) void save();
      active = false;
      if (timer) clearTimeout(timer);
      if (rateTimer) clearInterval(rateTimer);
      unsubscribePreview(); unsubscribeLibrary(); client?.close();
    };
  });
</script>

{#if missing}
  <section class="empty"><h1>This clip is no longer available</h1><a href="#/library">Back to Library</a></section>
{:else}
  <section bind:this={section} style:--preview-cap={docked ? "max(160px, 100dvh - 300px)" : previewHeight ? `min(${previewHeight}px, max(120px, 100dvh - 480px))` : undefined} class="develop" class:docked aria-label="Clip preview">
    <div class="viewer">
      {#if message}<p role="status">{message}</p>{/if}
      {#if problem && !problem.field}<p role="alert">{problem.message}</p>{/if}
      {#if preview.problem}<p role="alert">{preview.problem.message}</p>{/if}
      {#if notPlanned}<p role="status" class="not-stabilised">Not stabilised: {notPlanned}</p>{/if}
      <Viewport compact {docked} {ondock} width={imageWidth} height={imageHeight}>
      <NativeViewport {client} overlay={overlayModel} crop={outline} diagnostic={clippingActive}>
        <Warp corners={warping?.[preview.header?.index ?? index] ?? null} width={imageWidth} height={imageHeight}>
          <PreviewCanvas native={!!preview.header?.display} placeholder={thumbnailUrl(id)} scrubbing={preview.kind === "cached"} onpainter={(value) => { paintAfter = value; }} frame={preview.frame} index={preview.header?.index} />
          {#if recipe}<ClippingOverlay {id} recipe={shown(recipe)} mode={clippingMode} active={clippingActive} index={preview.header?.index ?? -1} />{/if}
        </Warp>
        {#if editing}<CropEditor width={whole.width} height={whole.height} box={editing.box} aspect={editing.aspect} onchange={croptools.set} />
        {:else if cropGuide > 1 && !warping}<CropGuide width={imageWidth} height={imageHeight} crop={cropGuide} corners={keptNow?.corners.length ? keptNow.corners : null} index={preview.header?.index ?? index} planning={!!keptKey && !keptNow} refining={keptNow?.refining ?? false} reason={keptNow?.reason ?? null} />{/if}
      </NativeViewport>
      {#snippet overlay()}{#if overlayModel}<ChartOverlay model={overlayModel} index={preview.header?.index ?? 0} />{/if}{/snippet}
      {#snippet timeline()}{#if ready || timelineReady}<TrimBar edge {index} {count} start={trimStart} end={trimEnd} {rate} {peaks} onseek={(value) => { transport?.pause(); scrub(value); }} onrest={() => seek(index)} ontrim={mark} onpreview={(value) => { transport?.pause(); scrub(value); }} />{/if}{/snippet}
      {#snippet tools()}{#if recipe && (ready || timelineReady)}<OverlaySelector {id} {recipe} raw={geometry?.kind === "raw"} gyro={geometry?.gyro ?? false} {count} onmodel={(value) => overlayModel = value} />{/if}
        {#if keptKey}<button class="secondary suggest toggle" aria-pressed={$stabilisedView && !notPlanned} disabled={!!notPlanned} onclick={() => stabilisedView.update(on => !on)} title={notPlanned ? `Cannot show the picture as Resolve will stabilise it: ${notPlanned}` : keptNow?.corners.length || !$stabilisedView ? "Show the picture as Resolve will stabilise it, or as recorded with the kept frame outlined" : "Shown as recorded until the stabilisation is planned"}>Stabilised</button>{/if}
        {#if geometry?.kind === "raw"}<label class="resolution"><span class="sr-only">Sensor clipping</span><select aria-label="Sensor clipping" value={clippingMode} onchange={async (event) => {
          clippingMode = event.currentTarget.value as "off" | "any" | "all";
          transport?.pause(); await tick(); seek(index);
        }}><option value="off">Clipping off</option><option value="any">Pink: any sensor channel</option><option value="all">Pink: all RGB channels</option></select></label>{/if}
        <label class="resolution"><span class="sr-only">Preview resolution</span><select aria-label="Preview resolution" value={resolution} onchange={(event) => {
          setClipView(id, { resolution: event.currentTarget.value as "adaptive" | "source" });
          if (client) client.sourceResolution = resolution === "source";
          if (ready && recipe) client?.request("recipe", { recipe: shown(recipe), index, fast: true, playback: playing || preparing, ahead: playing || preparing ? 3 : 1 });
        }}><option value="adaptive">Adaptive</option><option value="source" disabled={!supportsSource}>Source</option></select></label>
      {/snippet}
      {#snippet controls()}
        {#if ready || timelineReady}<Transport preloadAudio showTimeline={false} bind:this={transport} {id} {title} {index} {count} {rate} start={trimStart} end={trimEnd} {reached} onseek={seek} onscrub={scrub} onrest={() => seek(index)} ontrim={mark} onclock={(value) => client?.present(value)} onplaying={playbackChanged} onprepare={preparePlayback}>
        </Transport>{/if}
      {/snippet}
      </Viewport>
      {#if (ready || timelineReady) && $settings?.show_preview_stats}<PreviewCache {client} />{/if}
      <div class="preview-status">{#if preview.kind === "draft" && (playing || preview.header?.source)}<span>{playing ? "Playing" : "Source preview"}</span>{:else if refining}<span title="The picture is a quick draft. This frame is being developed at full quality, for paused inspection, and replaces the draft when it is ready.">Quick draft shown · full quality in progress</span>{:else}<span></span>{/if}{#if $settings?.show_preview_stats}<span title="Time taken to develop the frame shown">{preview.ms.toFixed(1)} ms</span>{/if}
        </div>
      {#if clippingMode !== "off"}<p class="notes">Pink marks recorded samples at or above sensor white, before correction. {clippingMode === "any" ? "Any RGB channel" : "All RGB channels"} in the CFA cell; either green site counts as green. Shown while paused only.</p>{/if}
      {#if resolution === "source"}<p class="notes">Source uses full image dimensions. Adaptive uses smaller frames for faster playback.</p>{/if}
      {#if $settings?.show_preview_stats && preview.header?.backend}<p class="notes" title={preview.header.backend_reason}>{preview.header.width} x {preview.header.height} · {preview.header.backend}{#if preview.header.display} · Native viewport{/if}{#if preview.header.backend === "CPU" && preview.header.backend_reason} · {preview.header.backend_reason}{/if}</p>{/if}
      {#if preview.header?.notes?.length}<p class="notes">{preview.header.notes.join("; ")}</p>{/if}
    </div>
    {#snippet settingsPanel()}
      {#if recipe}
        <DevelopSelection {id} values={layer.values} onreset={() => void reset()} {trimNote} suggestingTrim={suggesting}
          onsuggesttrim={(ready || timelineReady) && geometry?.gyro ? () => void suggestTrim() : undefined} />
        <DevelopPanel {sources} {colourNote} {detectedTransfer} values={layer.values} {raw} {croptools} cropediting={editing} gyro={geometry?.gyro ?? false} blurredSeconds={geometry?.blurred_s ?? null} {orientationHint} onclear={clearRaw} {recipe} {problem} onchange={setValue} oncolour={(values) => setValues("develop", values)} />
      {:else}<p role="status">Reading Resolve settings…</p>{/if}
    {/snippet}
    {#if !docked}<PreviewDivider bind:height={previewHeight} pane={section} picture={() => section?.querySelector(".picture") ?? null} />{/if}{@render inspector(settingsPanel, savingText)}
  </section>
{/if}
<style>
  /* The picture stays in view and the settings scroll below it; the divider between them sets the
     picture's height (Viewport's --cap), so the user chooses how the pane is shared. */
  .develop { display: grid; grid-template-columns: minmax(0, 1fr); position: sticky; top: 0; align-self: start; min-height: 0; max-height: calc(100dvh - 180px); grid-template-rows: auto auto minmax(0, 1fr); overflow: hidden; min-width: 0; }
  .develop :global(.inspector) { position: static; max-height: none; overflow: auto; }
  /* Docked: the picture where the clip list was, the settings in the column the pane had, from the top. */
  .develop.docked { grid-template-columns: minmax(0, 1fr) clamp(30em, 40em, 45%); grid-template-rows: minmax(0, 1fr); column-gap: 16px; }
  .develop.docked .viewer { grid-column: 1; grid-row: 1; overflow: auto; }
  .develop.docked :global(.inspector) { grid-column: 2; grid-row: 1; }
  /* Overlay scrollbars do not reserve a gutter. Keep the windowed trim grip clear of that edge;
     fullscreen promotes the viewport itself, so this padding stays outside the full-screen picture. */
  .viewer { padding: 0 0 10px; padding-inline-end: 16px; min-width: 0; }
  .preview-status { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  h1 { font-size: 16px; margin: 8px 0 0; overflow-wrap: anywhere; }
  .preview-status, .notes { color: var(--muted); font-size: 12px; }
  .preview-status { margin: 5px 0; flex-wrap: wrap; font-size: 10px; }
  .resolution select { max-width: 88px; padding: 3px 5px; font-size: 11px; }
  .suggest { padding: 3px 7px; font-size: 11px; }
  .toggle[aria-pressed="true"] { border-color: var(--accent); color: var(--accent); }
  .toggle:disabled { opacity: .5; cursor: not-allowed; }
  .not-stabilised { margin: 0 0 6px; color: var(--warn); font-size: 12px; }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
  a { color: var(--accent); } .empty { padding: 32px; }
  @media (max-width: 900px) { .develop { position: static; max-height: none; overflow: visible; } .develop :global(.divider) { display: none; }
    .develop.docked { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto auto; } .develop.docked :global(.inspector) { grid-column: 1; grid-row: 2; } }
</style>
