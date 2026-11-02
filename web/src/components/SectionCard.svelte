<script lang="ts">
  import type { Snippet } from "svelte";
  import Icon from "./Icon.svelte";
  import { openSections, setSectionOpen } from "../lib/sections";
  // Part of the panel under the preview that folds to one line: its title and what it is set to. A switch in
  // the heading (`control`) stays within reach while folded. Each section remembers whether it is open.
  let { id, title, summary = "", initiallyOpen = false, control, children }: {
    id: string; title: string; summary?: string; initiallyOpen?: boolean; control?: Snippet; children: Snippet;
  } = $props();
  const identity = $props.id();
  let open = $derived($openSections[id] ?? initiallyOpen);
</script>

<section class="card" class:open aria-labelledby={`${identity}-title`}>
  <div class="head">
    <button type="button" class="fold" aria-expanded={open} aria-controls={`${identity}-body`} onclick={() => setSectionOpen(id, !open)}>
      <span class="chevron"><Icon name="chevron" /></span>
      <span class="title" id={`${identity}-title`}>{title}</span>
      {#if summary}<span class="summary">{summary}</span>{/if}
    </button>
    {#if control}<div class="control">{@render control()}</div>{/if}
  </div>
  <div class="body" id={`${identity}-body`} hidden={!open}>{#if open}{@render children()}{/if}</div>
</section>

<style>
  .card { min-width: 0; border: 1px solid var(--line); border-radius: 8px; background: var(--card); }
  .head { display: flex; align-items: center; gap: 8px; padding-right: 10px; }
  /* An open section's heading takes the selected clip's tint and a teal title, to stand apart from its contents. */
  .open .head { border-radius: 7px 7px 0 0; background: color-mix(in srgb, var(--accent) 13%, var(--card)); }
  .fold { flex: 1; min-width: 0; display: flex; align-items: center; gap: 8px; padding: 9px 4px 9px 10px; border: 0; border-radius: 8px; background: transparent; color: var(--text); font-weight: 400; text-align: left; }
  .chevron { display: inline-flex; flex: none; color: var(--muted); transition: transform .12s; }
  .chevron :global(svg) { width: 14px; height: 14px; }
  .open .chevron { color: var(--accent); transform: rotate(90deg); }
  .title { flex: none; font-size: 13px; font-weight: 600; }
  .open .title { color: var(--accent); }
  .summary { flex: 1; min-width: 0; margin-left: 8px; overflow: hidden; color: var(--muted); font-size: 11.5px; text-align: right; white-space: nowrap; text-overflow: ellipsis; }
  .open .summary { color: color-mix(in srgb, var(--text) 30%, var(--muted)); }
  .control { display: flex; flex: none; align-items: center; }
  .body { display: grid; gap: 12px; min-width: 0; padding: 12px; border-top: 1px solid color-mix(in srgb, var(--accent) 30%, var(--card)); font-size: 12px; }
  .body[hidden] { display: none; }
  @media (prefers-reduced-motion: reduce) { .chevron { transition: none; } }
</style>
