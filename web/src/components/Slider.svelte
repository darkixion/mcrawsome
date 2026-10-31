<script lang="ts">
  import InfoTip from "./InfoTip.svelte";
  let { label, value, min, max, step = 0.01, error, info, retainValue = false, disabled = false, onchange }: {
    label: string; value: number; min: number; max: number; step?: number; error?: string; info?: string;
    onchange: (value: number) => void;
    retainValue?: boolean; disabled?: boolean;
  } = $props();
  const id = $props.id();
</script>
<div class="control">
  <div class="head"><label for={id}>{label}</label>{#if info}<InfoTip {label} text={info} />{/if}<output for={id}>{value.toFixed(2)}</output></div>
  <input {id} aria-label={label} type="range" {min} {max} {step} {value} {disabled} aria-describedby={error ? `${id}-error` : undefined}
    oninput={(event) => { const next = Number(event.currentTarget.value); if (retainValue) event.currentTarget.value = String(value); onchange(next); }} />
  {#if error}<p id={`${id}-error`} role="alert">{error}</p>{/if}
</div>
<style>
  .head { display: flex; align-items: center; gap: 2px; }
  output { margin-left: auto; padding-left: 12px; }
  output { color: var(--muted); font-variant-numeric: tabular-nums; }
  input { width: 100%; accent-color: var(--accent); }
  p { color: var(--danger, #ef9a91); margin: 4px 0; }
</style>
