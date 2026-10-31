<script lang="ts">
  import { onMount } from "svelte";
  import { api, type ApiError, type Schemas } from "../lib/api";
  import { bridge } from "../lib/bridge";
  import ErrorNote from "./ErrorNote.svelte";
  import Icon from "./Icon.svelte";
  import { obscure } from "../lib/obscuring";
  import { sessionRequest } from "../lib/session-request";
  let { workspace, onrestore, beforechange = async () => true, shoot = null }: { workspace: Schemas["Workspace"]; onrestore: (session: Schemas["SessionView"]) => void; beforechange?: () => Promise<boolean>; shoot?: { label: string; paths: string[] } | null } = $props();
  let session = $state<Schemas["SessionView"] | null>(null);
  let path = $state("");
  let problem = $state<ApiError | null>(null);
  let busy = $state(false);
  let ready = $state(false);
  let saved = $state("");
  let timer: ReturnType<typeof setTimeout> | null = null;
  let pending: Promise<boolean> | null = null;
  let latest = "";
  let open = $state(false);
  let root: HTMLDetailsElement;
  let popup = $state<HTMLDivElement>();
  $effect(() => { if (open && popup) return obscure(popup); });
  $effect(() => { if (problem || session?.problem) open = true; });
  function outside(event: PointerEvent) { if (open && !root.contains(event.target as Node)) open = false; }
  function keyboard(event: KeyboardEvent) { if (event.key === "Escape" && open) { event.preventDefault(); event.stopPropagation(); open = false; root.querySelector("summary")?.focus(); } }

  function restore(value: Schemas["SessionView"]): void {
    saved = JSON.stringify(value.workspace); latest = saved;
    session = value; path = value.path ?? ""; onrestore(value);
  }
  async function persist(force = false): Promise<boolean> {
    if (timer) { clearTimeout(timer); timer = null; }
    if (pending) { if (!await pending) return false; }
    if (!ready || !latest || (!force && latest === saved)) return true;
    const body = latest;
    pending = (async () => {
      const result = await api.updateSession(JSON.parse(body));
      if (!result.ok) { problem = result; return false; }
      saved = body; session = result.data; problem = null; return true;
    })();
    try { return await pending; } finally { pending = null; }
  }
  $effect(() => {
    const body = JSON.stringify(workspace);
    if (!ready || busy) return;
    latest = body;
    if (body !== saved) {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => { timer = null; void persist(); }, 400);
    }
  });
  async function action(kind: "new" | "load" | "save" | "save-as" | "portable", recent?: string, paths?: string[]): Promise<void> {
    if (busy) return;
    busy = true; problem = null;
    try {
      if (!await beforechange()) return;
      latest = JSON.stringify(workspace);
      if (!(session?.problem && (kind === "new" || kind === "load")) && !await persist(true)) return;
      let chosen = recent ?? path;
      const save = kind === "save" || kind === "save-as" || kind === "portable";
      if (kind === "load" && !recent && $bridge?.api.choose_session) chosen = await $bridge.api.choose_session(false) ?? "";
      if (save && (kind !== "save" || !session?.path) && $bridge?.api.choose_session) chosen = await $bridge.api.choose_session(true) ?? "";
      if (kind !== "new" && !chosen) {
        if (!$bridge?.api.choose_session) problem = { ok: false, code: "input-unreadable", message: "Enter a full session file path" };
        return;
      }
      let result = kind === "new" ? await api.newSession() : kind === "load" ? await api.loadSession(chosen)
        : await api.saveSession(chosen, kind === "portable" ? true : kind === "save-as" ? false : undefined);
      if (!result.ok) { problem = result; return; }
      if ((kind === "new" || kind === "load") && paths?.length) {  // the session also holds these clips
        const opened = await api.openClips(paths);
        if (!opened.ok) { problem = opened; restore(result.data); return; }
        result = await api.session();
        if (!result.ok) { problem = result; return; }
      }
      if (kind === "new" || kind === "load") restore(result.data);
      else { session = result.data; path = result.data.path ?? ""; }
      open = false;
    } catch { problem = { ok: false, code: "input-unreadable", message: "The session file dialog could not be opened" }; }
    finally { busy = false; }
  }
  // A session file opened from the desktop loads as Load does, once the bar has its own session.
  $effect(() => {
    const request = $sessionRequest;
    if (!ready || busy || !request) return;
    sessionRequest.set(null);
    void action("load", request.path, request.paths);
  });
  onMount(() => {
    let alive = true;
    void api.session().then(result => {
      if (!alive) return;
      if (!result.ok) { problem = result; return; }
      if (!result.data.workspace) { problem = { ok: false, code: "internal-error", message: "The engine returned an invalid workspace" }; return; }
      session = result.data;
      if (result.data.workspace.paths?.length || result.data.path) restore(result.data);
      else saved = JSON.stringify(workspace);
      latest = saved; ready = true;
    });
    const leaving = () => {
      if (ready && latest) void fetch("/api/v1/session", { method: "PUT", headers: { "Content-Type": "application/json" }, body: latest, keepalive: true });
    };
    window.addEventListener("pagehide", leaving);
    return () => { alive = false; window.removeEventListener("pagehide", leaving); if (timer) clearTimeout(timer); void persist(); };
  });
</script>

<svelte:window onpointerdown={outside} onkeydown={keyboard} />
<details class="sessions" aria-label="Sessions" bind:this={root!} bind:open>
  <summary><Icon name="frames" /> Sessions <span aria-hidden="true">⌄</span></summary>
  <div class="popup" bind:this={popup}>
  <strong title={session?.path ?? "Auto-saved workspace"}>{session?.name ?? "Session"}</strong>
  <button class="quiet" disabled={!ready || busy} onclick={() => void action("new")}>New</button>
  {#if shoot?.paths.length}<button class="quiet" disabled={!ready || busy} title={`A new session holding the clips of ${shoot.label}`} onclick={() => void action("new", undefined, shoot.paths)}>New session from this shoot ({shoot.paths.length} clips)</button>{/if}
  <button class="quiet" disabled={!ready || busy} onclick={() => void action("load")}>Load</button>
  <button class="quiet" disabled={!ready || busy} onclick={() => void action("save")}>Save</button>
  <button class="quiet" disabled={!ready || busy} onclick={() => void action("save-as")}>Save As</button>
  <button class="quiet" disabled={!ready || busy} onclick={() => void action("portable")} title="Save paths relative to the session file. Move the session and recordings together with the same folder layout. Media is not copied.">Save with relative paths</button>
  <select aria-label="Recent sessions" disabled={!ready || busy || !session?.recent.length} value="" onchange={(event) => { if (event.currentTarget.value) void action("load", event.currentTarget.value); }}>
    <option value="">Recent sessions</option>
    {#each session?.recent ?? [] as recent}<option value={recent}>{recent.split("/").at(-1)}</option>{/each}
  </select>
  {#if !$bridge?.api.choose_session}<input aria-label="Session file path" placeholder="/path/name.mcrawsome-session.json" bind:value={path} />{/if}
  <span role="status" aria-label="Session saving">{busy ? "Saving workspace..." : !ready ? "Reading workspace..." : saved === JSON.stringify(workspace) ? "Workspace auto-saved" : "Workspace changed"}</span>
{#if session?.problem}<p role="alert">{session.problem}</p>{/if}
{#if session?.missing.length}<p role="status">Missing recordings: {session.missing.join(", ")}. Their references are kept in the session.</p>{/if}
{#if problem}<ErrorNote error={problem} />{/if}
  </div>
</details>

<style>
  .sessions { position: relative; z-index: 8; }
  summary { display: flex; align-items: center; gap: 6px; padding: 7px 10px; border-radius: 6px; background: var(--card); color: var(--text); cursor: pointer; list-style: none; font-size: 12px; }
  summary::-webkit-details-marker { display: none; }
  summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  summary :global(svg) { width: 16px; height: 16px; }
  .popup { position: absolute; top: calc(100% + 6px); left: 0; width: min(288px, calc(100vw - 72px)); display: grid; gap: 5px; padding: 12px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); box-shadow: 0 8px 24px #0006; }
  button { text-align: left; }
  strong { font-size: 12px; max-width: 180px; overflow: hidden; text-overflow: ellipsis; }
  button, select, input, span, p { font-size: 12px; }
  input { width: 100%; min-width: 0; }
  span, p { color: var(--muted); }
</style>
