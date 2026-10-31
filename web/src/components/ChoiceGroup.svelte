<script lang="ts" generics="T extends string">
  // A labelled set of radio choices: a compact segmented row, or a list with a description under each choice.
  type Choice = { value: T; label: string; description?: string };
  let { label, choices, value, onchange, layout = "list" }: {
    label: string; choices: Choice[]; value: T; onchange: (value: T) => unknown; layout?: "segmented" | "list";
  } = $props();
  const name = $props.id();
  let group: HTMLDivElement;

  async function choose(next: T): Promise<void> {
    await onchange(next);
    // A failed save leaves value unchanged, so put the browser's own selection back to match it.
    for (const input of group.querySelectorAll<HTMLInputElement>("input")) input.checked = input.value === value;
  }
</script>
<div bind:this={group} class="choices {layout}" role="radiogroup" aria-label={label}>
  {#each choices as choice (choice.value)}
    <label class="choice">
      <input type="radio" {name} value={choice.value} checked={choice.value === value} onchange={() => void choose(choice.value)} />
      <span class="text"><span class="name">{choice.label}</span>{#if choice.description && layout === "list"}<span class="description">{choice.description}</span>{/if}</span>
    </label>
  {/each}
</div>
<style>
  .choices { min-width: 0; font-size: 13px; }
  .list { display: grid; gap: 4px; }
  .list .choice { display: flex; align-items: flex-start; gap: 9px; padding: 7px 10px; border: 1px solid transparent; border-radius: 7px; cursor: pointer; }
  .list .choice:hover { border-color: var(--line); }
  .list .choice:has(input:checked) { border-color: color-mix(in srgb, var(--accent) 55%, var(--line)); background: color-mix(in srgb, var(--accent) 8%, transparent); }
  .list input { flex: 0 0 auto; margin: 2px 0 0; accent-color: var(--accent); }
  .text { display: grid; gap: 2px; min-width: 0; }
  .description { color: var(--muted); font-size: 12px; line-height: 1.45; }
  .segmented { display: inline-flex; max-width: 100%; border: 1px solid var(--line); border-radius: 7px; overflow: hidden; }
  .segmented .choice { position: relative; display: flex; align-items: center; min-height: 30px; padding: 0 16px; cursor: pointer; }
  .segmented .choice + .choice { border-left: 1px solid var(--line); }
  .segmented .choice:hover { background: color-mix(in srgb, var(--text) 5%, transparent); }
  .segmented .choice:has(input:checked) { background: var(--accent); color: var(--accent-ink); font-weight: 500; }
  .segmented input { position: absolute; inset: 0; margin: 0; opacity: 0; cursor: pointer; }
  .segmented .choice:has(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: -2px; }
  @media (forced-colors: active) {
    .segmented .choice:has(input:checked) { forced-color-adjust: none; background: Highlight; color: HighlightText; }
    .list .choice:has(input:checked) { border-color: Highlight; }
  }
</style>
