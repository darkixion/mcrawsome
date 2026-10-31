<script lang="ts">
  import { onMount } from "svelte";
  import { api, type ApiError, type Schemas } from "../lib/api";
  import ErrorNote from "./ErrorNote.svelte";
  import InfoTip from "./InfoTip.svelte";

  let { develop, note = null, detectedTransfer = null, onchange }: {
    develop: Record<string, unknown>;
    note?: string | null;
    detectedTransfer?: string | null;
    onchange: (values: Record<string, unknown>) => void;
  } = $props();
  let choices = $state<Schemas["ColourChoices"] | null>(null);
  let problem = $state<ApiError | null>(null);
  const id = $props.id();
  let legacy = $derived(choices?.legacy[String(develop.input_encoding ?? "auto")]);
  let gamut = $derived(String(legacy?.gamut ?? develop.input_gamut ?? "auto"));
  let transfer = $derived(String(legacy?.transfer ?? develop.input_transfer ?? "auto"));
  function change(values: Record<string, unknown>) {
    const next = {
      input_encoding: "auto", input_gamut: gamut, input_transfer: transfer,
      input_ei: develop.input_ei ?? 800, native_as: develop.native_as ?? "unset", ...values,
    };
    if (next.input_transfer !== "auto" && next.input_transfer !== "logc3") next.input_ei = 800;
    if (next.input_gamut !== "camera-native") next.native_as = "unset";
    onchange(next);
  }
  onMount(() => {
    let current = true;
    void api.colour().then(result => {
      if (!current) return;
      if (result.ok) choices = result.data;
      else problem = result;
    });
    return () => { current = false; };
  });
</script>
<div class="input-colour">
  <div class="heading">Input colour <InfoTip label="input colour" text="Auto reads MotionCam's file-name tokens, or supported standard metadata for other videos. File-name identification is inferred: if a file has been renamed, check the source colour settings and override them as needed. These describe the recording, rather than the export. ARRI LogC3 uses the exposure index recorded in camera." /></div>
  {#if problem}<ErrorNote error={problem} />{/if}
  {#if note && (gamut === "auto" || transfer === "auto")}<p role="status" class="colour-note">{note}</p>{/if}
  <fieldset disabled={!choices}>
    <label>Input colour space<select value={gamut} onchange={(event) => change({ input_gamut: event.currentTarget.value })}>
      <option value="auto">Auto (file name or metadata)</option>
      {#each choices?.gamuts ?? [] as choice}<option value={choice.id}>{choice.name}</option>{/each}
    </select></label>
    <label>Input transfer function<select value={transfer} onchange={(event) => change({ input_transfer: event.currentTarget.value })}>
      <option value="auto">Auto (file name or metadata)</option>
      {#each choices?.transfers ?? [] as choice}<option value={choice.id}>{choice.name}</option>{/each}
    </select></label>
    {#if transfer === "logc3" || (transfer === "auto" && detectedTransfer === "logc3")}
      <div class="field">
        <div class="field-heading"><label for={`${id}-ei`}>ARRI exposure index</label><InfoTip label="ARRI exposure index" text="For ARRI LogC3 only: choose the exposure index used to encode the recording, normally EI 800. This selects the LogC3 curve variant; it is separate from the exposure adjustment. MotionCam's file name does not record EI." /></div>
        <select id={`${id}-ei`} value={Number(develop.input_ei ?? 800)} onchange={(event) => change({ input_ei: Number(event.currentTarget.value) })}>
          {#each choices?.logc3_ei ?? [] as ei}<option value={ei}>{ei}</option>{/each}
        </select>
      </div>
    {/if}
    {#if gamut === "camera-native"}
      <label>Camera Native interpretation<select value={String(develop.native_as ?? "unset")} onchange={(event) => change({ native_as: event.currentTarget.value })}>
        <option value="unset">Require camera colour matrix</option><option value="ap1">Assume ACES AP1</option>
      </select></label>
      <InfoTip label="Camera Native" text="Camera Native needs the camera's measured colour matrix. Assume ACES AP1 is an explicit approximation and can change colours. No matrix is invented automatically." />
    {/if}
    <button type="button" onclick={() => change({ input_gamut: "auto", input_transfer: "auto", input_ei: 800, native_as: "unset" })}>Use Auto colour</button>
  </fieldset>
</div>
<style>
  .input-colour { grid-column: 1 / -1; min-width: 0; }
  .heading { display: flex; align-items: center; gap: 6px; font-size: 13px; margin-bottom: 8px; }
  .colour-note { font-size: 12px; color: var(--warn); line-height: 1.5; }
  fieldset { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; border: 0; padding: 0; margin: 0; min-width: 0; }
  label { display: grid; gap: 6px; font-size: 13px; min-width: 0; }
  .field { display: grid; gap: 6px; min-width: 0; }
  .field-heading { display: flex; align-items: center; gap: 6px; }
  select { width: 100%; min-width: 0; }
  button { align-self: end; justify-self: start; }
  @media (max-width: 520px) { fieldset { grid-template-columns: minmax(0, 1fr); } }
</style>
