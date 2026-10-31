<script lang="ts" module>
  const STORAGE = "mcrawsome.preview-height";
  export function savedPreviewHeight(): number | null {
    try {
      const value = Number(localStorage.getItem(STORAGE));
      return Number.isFinite(value) && value >= MIN ? value : null;
    } catch { return null; }
  }
  const MIN = 120;
</script>
<script lang="ts">
  // Between the preview and the settings in the Library pane: dragging it sets the picture's height
  // (Viewport's --cap), and the settings below scroll on their own, so the picture stays in view.
  let { height = $bindable(), pane, picture }: { height: number | null; pane: HTMLElement | undefined; picture: () => HTMLElement | null } = $props();
  const SETTINGS = 160; // the settings always keep at least this much of the pane

  function current(): number { return picture()?.getBoundingClientRect().height ?? 240; }
  function limit(): number {
    const shown = picture()?.getBoundingClientRect().height ?? 0;
    const rest = (pane?.querySelector(".viewer")?.getBoundingClientRect().height ?? 0) - shown;
    return Math.max(MIN, (pane?.clientHeight ?? 600) - rest - SETTINGS);
  }
  function set(value: number): void {
    height = Math.round(Math.max(MIN, Math.min(value, limit())));
    try { localStorage.setItem(STORAGE, String(height)); } catch { /* The size is a convenience. */ }
  }
  function drag(event: PointerEvent): void {
    if (event.button !== 0) return;
    const handle = event.currentTarget as HTMLElement, start = event.clientY, from = current();
    handle.setPointerCapture(event.pointerId);
    const move = (next: PointerEvent) => set(from + next.clientY - start);
    const end = () => { handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", end); handle.removeEventListener("pointercancel", end); };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
    event.preventDefault();
  }
  function key(event: KeyboardEvent): void {
    const step = event.shiftKey ? 80 : 20;
    if (event.key === "ArrowUp") set(current() - step);
    else if (event.key === "ArrowDown") set(current() + step);
    else if (event.key === "Home") set(MIN);
    else if (event.key === "End") set(limit());
    else return;
    event.preventDefault();
  }
</script>
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div class="divider" role="separator" aria-orientation="horizontal" aria-label="Resize preview" title="Drag to share the pane between the preview and the settings"
  tabindex="0" aria-valuemin={MIN} aria-valuenow={Math.round(height ?? current())} onpointerdown={drag} onkeydown={key} ondblclick={() => { height = null; try { localStorage.removeItem(STORAGE); } catch { /* ignored */ } }}>
  <span></span>
</div>
<style>
  .divider { display: grid; place-items: center; height: 10px; margin: 2px 16px 4px 0; cursor: row-resize; touch-action: none; border-radius: 4px; }
  .divider span { width: 36px; height: 4px; border-radius: 2px; background: var(--line); }
  .divider:hover span, .divider:focus-visible span { background: var(--accent); }
  .divider:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
</style>
