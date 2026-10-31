<script lang="ts">
  import type { Schemas } from "../lib/api";
  let { checks }: { checks: Schemas["Check"][] } = $props();
</script>
<section aria-label="System check" class="system-check">
  <h2>System check</h2>
  <table>
    <tbody>
      {#each checks as check (check.name)}
        <tr>
          <td><span class="mark {check.status}">{check.status}</span></td>
          <th scope="row">{check.name}</th>
          <td>{check.detail}{#if check.fix}<span class="fix">{check.fix}</span>{/if}</td>
        </tr>
      {/each}
    </tbody>
  </table>
</section>
<style>
  .system-check { margin-top: 24px; }
  h2 { font-size: 16px; margin: 0 0 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td, th { padding: 10px 12px 10px 0; border-top: 1px solid var(--line); text-align: left; vertical-align: top; overflow-wrap: anywhere; }
  th { font-weight: 500; min-width: 100px; }
  .mark { display: inline-block; min-width: 40px; padding: 2px 6px; border-radius: 6px; font-size: 11px; text-align: center; text-transform: uppercase; }
  .mark.ok { background: var(--accent); color: var(--accent-ink); }
  .mark.warn { background: var(--warn); color: var(--accent-ink); }
  .mark.fail { background: var(--fail); color: var(--text); }
  .fix { display: block; margin-top: 4px; color: var(--muted); }
</style>
