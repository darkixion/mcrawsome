<script lang="ts">
  // The engine's error, shown where the problem is: its message, with its code for reference.
  let { error }: { error: { code: string; message: string } } = $props();
</script>

<p class="error" role="alert">
  <span class="message">{error.message}</span>
  <span class="code">{error.code}</span>
</p>

<style>
  .error {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 8px;
    margin: 6px 0 0;
    color: var(--fail);
    font-size: 12px;
  }
  .code {
    color: var(--muted);
    font-family: var(--mono);
  }
</style>
