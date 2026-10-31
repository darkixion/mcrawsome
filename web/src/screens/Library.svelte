<script lang="ts">
  import { onMount, tick } from "svelte";
  import { mediaUse } from "../lib/events";
  import SessionBar from "../components/SessionBar.svelte";
  import { FACETS, facetLabel, facetValues, matchesFacets, type Facets } from "../lib/library-filters";
  import Icon from "../components/Icon.svelte";
  import Develop from "./Develop.svelte";
  import ClipContextMenu from "../components/ClipContextMenu.svelte";
  import VignetteActions from "../components/VignetteActions.svelte";
  import TrashConfirm from "../components/TrashConfirm.svelte";
  import TimelineRates from "../components/TimelineRates.svelte";
  import { applyAdjustments } from "../lib/bulk-adjustments";
  import type { SelectionAction, ProfileAction } from "../lib/vignette";
  import ClipInspector from "../components/ClipInspector.svelte";
  import ClipGrid from "../components/ClipGrid.svelte";
  import SendActivity from "../components/SendActivity.svelte";
  import { sendActivity, sendRaw, sendTargets, restoreSendHistory, type SendTarget } from "../lib/send";
  import ErrorNote from "../components/ErrorNote.svelte";
  import FolderBar from "../components/FolderBar.svelte";
  import { api, type ApiError, type Schemas } from "../lib/api";
  import { clips, clipViews, openedClips, openedPaths, showOpened, mediaView, previewDocked, libraryProblem, loadLibrary, missingFolders, type ClipSummary } from "../lib/library";
  import { bridge } from "../lib/bridge";
  import { selection } from "../lib/selection";
  import { settings } from "../lib/settings";
  import { warmPreview, discardWarmPreview } from "../lib/preview-opening";
  import { annotationsFor, annotationRatings, annotationTexts, clearAnnotationSessions, flushAnnotations } from "../lib/annotations";
  import { withGroups } from "../lib/clip-groups";
  import ClipLists from "../components/ClipLists.svelte";

  let view = $derived($mediaView ?? ($settings?.folders?.length ? "library" : "opened"));
  let items = $derived.by(() => {
    const listed = (view === "opened" ? $openedClips : $clips)?.map(clip => ({ ...clip, rating: clip.id ? $annotationRatings[clip.id] ?? clip.rating ?? 0 : 0 }));
    return listed ? withGroups(listed) : null;
  });
  let facets = $state<Facets>({});
  let filterNow = $state(new Date());
  function restoreSession(session: Schemas["SessionView"]): void {
    clearAnnotationSessions();
    clipViews.set(session.workspace.views ?? {});
    profileRevision += 1;
    showOpened(session.clips, session.workspace.paths ?? []);
    selection.set(session.workspace.selection ?? []); focused = session.workspace.focused ?? null;
    search = session.workspace.search ?? ""; filter = session.workspace.kind ?? "all";
    facets = session.workspace.facets ?? {};
    mediaView.set("opened"); menu = null;
  }
  async function beforeSessionChange(): Promise<boolean> {
    if (!await saveDevelop()) return false;
    if (!await flushAnnotations()) {
      problem = { ok: false, code: "output-unwritable", message: "Could not save clip details. Check the Notes tab before changing sessions or clearing the list." };
      return false;
    }
    return true;
  }
  let opening = $state(false);
  let openProblem = $state<ApiError | null>(null);
  let filePaths = $state("");
  let draggingFiles = $state(false);
  let dragDepth = 0;
  function resetDrag(): void { dragDepth = 0; draggingFiles = false; }
  // The native video surface follows the picture; moving it between columns can keep its size.
  function toggleDock(): void {
    previewDocked.update(docked => !docked);
    void tick().then(() => window.dispatchEvent(new Event("resize")));
  }
  function fileDrag(event: DragEvent): boolean {
    return [...(event.dataTransfer?.types ?? [])].some(type => type === "Files" || type === "text/uri-list");
  }
  function changeView(value: "library" | "opened"): void {
    mediaView.set(value); selection.set([]); focused = null; menu = null;
    if (value === "opened") void loadOpened();
  }
  async function loadOpened(): Promise<void> {
    const result = await api.openedClips();
    if (result.ok) showOpened(result.data.clips);
  }
  async function openFiles(paths: string[]): Promise<void> {
    if (!paths.length || opening) return;
    opening = true; openProblem = null;
    try {
      const result = await api.openClips(paths);
      if (result.ok) { showOpened(result.data.clips); filePaths = ""; }
      else openProblem = result;
    } finally { opening = false; }
  }
  async function chooseFiles(): Promise<void> {
    try { await openFiles(await $bridge?.api.choose_files?.() ?? []); }
    catch { openProblem = { ok: false, code: "input-unreadable", message: "The file dialog could not be opened" }; }
  }
  async function clearOpened(): Promise<void> {
    if (opening || !await beforeSessionChange()) return;
    const result = await api.clearOpenedClips();
    if (result.ok) { showOpened([], []); selection.set([]); focused = null; } else openProblem = result;
  }
  async function removeSelected(): Promise<void> {
    if (view === "library" || opening) return;
    const chosen = (items ?? []).filter(clip => clip.id && $selection.includes(clip.id));
    const ids = new Set(chosen.map(clip => clip.id!));
    const paths = [...new Set(chosen.flatMap(clip => [clip.path, ...clip.paths, ...(clip.canonical_paths ?? [])]))];
    if (!paths.length || !await beforeSessionChange()) return;
    opening = true; openProblem = null;
    try {
      const remainingSelection = $selection.filter(id => !ids.has(id));
      const remainingFocus = focused && ids.has(focused) ? remainingSelection.at(-1) ?? null : focused;
      const removed = new Set(paths);
      const result = await api.removeOpenedClips(paths);
      if (result.ok) {
        showOpened(result.data.clips, $openedPaths.filter(path => !removed.has(path)));
        selection.set(remainingSelection); focused = remainingFocus;
      } else openProblem = result;
      anchor = null;
    } finally { opening = false; }
  }
  function drop(event: DragEvent): void {
    event.preventDefault();
    if ($bridge?.api.choose_files) return; // Native drop supplies full paths through the desktop bridge.
    const uris = event.dataTransfer?.getData("text/uri-list").split(/\r?\n/).filter(line => line && !line.startsWith("#")) ?? [];
    try {
      if (!uris.length) throw Error("Your browser hides local file paths. Use the desktop app or enter the paths below.");
      const paths = uris.map(uri => { const url = new URL(uri); if (url.protocol !== "file:" || (url.hostname && url.hostname !== "localhost")) throw Error("Drop local files from this computer"); return decodeURIComponent(url.pathname); });
      void openFiles(paths);
    } catch (error) { openProblem = { ok: false, code: "input-unreadable", message: String(error).replace(/^Error: /, "") }; }
  }

  const FILTERS = [{ value: "all", label: "All" }, { value: "raw", label: "RAW" }, { value: "log", label: "LOG" }] as const;
  let filter = $state<"all" | "raw" | "log">("all");
  const sort = "captured" as const;
  let filtersOpen = $state(false);
  let search = $state("");
  let anchor = $state<string | null>(null); // the clip a Shift click extends from
  let focused = $state<string | null>(null); // the clip in the details panel
  let workspace = $derived({ paths: view === "opened" ? $openedPaths : (items ?? []).map(clip => clip.path), selection: $selection, focused, search, kind: filter, sort, facets, views: $clipViews });
  let newFolder = $state("");
  let readingFolder = $state("");
  let problem = $state<ApiError | null>(null);
  let previewPlaying = $state(false);
  let inspectorTab = $state("develop");
  let flushDevelop: (() => Promise<boolean>) | null = null;

  async function saveDevelop(): Promise<boolean> {
    if (!flushDevelop || await flushDevelop()) return true;
    problem = { ok: false, code: "output-unwritable", message: "Could not save Resolve settings. Check Resolve Settings before mounting or sending." };
    return false;
  }

  let savingForSend = false;
  async function sendClips(ids: string[], target: SendTarget = sendTargets[0]!): Promise<void> {
    if (savingForSend || mountBusy) return;
    const ready = (items ?? []).filter(clip => clip.id && ids.includes(clip.id) && clip.kind === "raw" && clip.status === "ready");
    ready.sort(byCaptured);
    savingForSend = true;
    try {
      if (!await saveDevelop()) return;
      for (const clip of ready) if (!await annotationsFor(clip.id!).flush()) {
        problem = { ok: false, code: "output-unwritable", message: "Could not save clip details. Resolve was not sent any clips. Check the clip details editor and try again." };
        return;
      }
      problem = null;
      rateCheck = null;
      if (target.mode === "timeline" && ready.length) {
        // A clip at a rate a timeline can't take is named before anything is mounted, with an offer to fix it.
        const rates = await api.timelineRates(ready.map(clip => clip.id!));
        if (!rates.ok) { problem = rates; return; }
        if (rates.data.clips.some(clip => !clip.usable)) { rateCheck = { clips: rates.data.clips, ids, target }; return; }
      }
      await sendRaw(ready.map(clip => ({ id: clip.id!, name: clip.name })), target, mountReady);
    } finally { savingForSend = false; }
  }
  let rateCheck = $state<{ clips: Schemas["TimelineRate"][]; ids: string[]; target: SendTarget } | null>(null);
  async function useWholeRates(changed: string[]): Promise<string> {
    const check = rateCheck;
    if (!check || !await saveDevelop()) return "Could not save Resolve settings. No frame rates were changed.";
    const failed = (await applyAdjustments(changed, { develop: { mount_frame_rate: "integer" } })).filter(result => !result.ok);
    if (details?.id && changed.includes(details.id)) profileRevision += 1; // the inspector shows the new rate
    void loadLibrary(); // the cards show the clips' own settings
    if (failed.length) {
      const names = failed.map(result => check.clips.find(clip => clip.id === result.id)?.name ?? result.id);
      return `Could not change the frame rate of ${names.join(", ")}: ${failed[0]!.message}`;
    }
    rateCheck = null;
    await sendClips(check.ids, check.target);
    return "";
  }
  let mounts = $state<Schemas["MediaListing"] | null>(null);
  let mountRefresh = 0;
  let preparingSources = $state<string[]>([]);
  function isMounting(clip: ClipSummary): boolean {
    const sources = [clip.path, ...clip.paths, ...(clip.canonical_paths ?? [])];
    return preparingSources.some(source => sources.includes(source)) || !!($sendActivity?.busy && $sendActivity.entries.some(entry =>
      entry.id === clip.id && (entry.preparation === "queued" || entry.preparation === "mounting")));
  }
  async function loadMounts(): Promise<void> {
    const asked = ++mountRefresh;
    const result = await api.mounts();
    if (result.ok && asked === mountRefresh) mounts = result.data;
  }
  async function mountReady(record?: Schemas["MountedSequence"]): Promise<void> {
    if (record?.source && mounts) {
      ++mountRefresh;
      mounts = { ...mounts, mounts: [...mounts.mounts.filter(mount => mount.id !== record.id), record] };
    } else await loadMounts();
  }

  let unmountResults = $state<Schemas["UnmountResult"][]>([]);
  async function unmountAll(): Promise<void> {
    if (mountBusy) return;
    mountBusy = true; unmountResults = [];
    try {
      const result = await api.unmountAll();
      if (result.ok) unmountResults = result.data.results; else problem = result;
      await loadMounts();
    } finally { mountBusy = false; }
  }
  let mountBusy = $state(false);
  async function mountClips(chosen: ClipSummary[]): Promise<void> {
    if (mountBusy || savingForSend || $sendActivity?.busy || !chosen.length) return;
    mountBusy = true; problem = null;
    try {
      if (!await saveDevelop()) return;
      preparingSources = chosen.filter(clip => clip.id && clip.kind === "raw" && clip.status === "ready").map(clip => clip.path);
      for (const clip of chosen) {
        if (!clip.id || clip.kind !== "raw" || clip.status !== "ready") continue;
        const result = await api.mount(clip.id);
        if (!result.ok) { problem = result; break; }
        await mountReady(result.data);
        preparingSources = preparingSources.filter(source => source !== clip.path);
      }
      await loadMounts();
    } finally { preparingSources = []; mountBusy = false; }
  }
  function clipMounts(clip: ClipSummary): Schemas["MountedSequence"][] {
    const sources = new Set([clip.path, ...clip.paths, ...(clip.canonical_paths ?? [])]);
    return mounts?.mounts.filter(mount => sources.has(mount.source)).map(mount => ({
      ...mount, in_use: Object.hasOwn($mediaUse, mount.id) ? $mediaUse[mount.id] : mount.in_use,
    })) ?? [];
  }
  async function mountAction(records: Schemas["MountedSequence"][], action: "unmount" | "remount"): Promise<void> {
    if (mountBusy) return;
    mountBusy = true;
    problem = null;
    if (action === "remount") preparingSources = records.map(record => record.source);
    try {
      for (const record of records) {
        const result = await api[action](record.id);
        if (!result.ok) { problem = result; break; }
        if ("source" in result.data) await mountReady(result.data);
        else await loadMounts();
        preparingSources = preparingSources.filter(source => source !== record.source);
      }
      await loadMounts();
    } finally { preparingSources = []; mountBusy = false; }
  }

  let menu = $state<{ clip: ClipSummary; x: number; y: number } | null>(null);
  let profileAction = $state<{ action: ProfileAction; chosen: ClipSummary[]; source: ClipSummary | null } | null>(null);
  let profileRevision = $state(0);
  let trashing = $state<ClipSummary[] | null>(null);
  function context(clip: ClipSummary, event: MouseEvent | KeyboardEvent): void {
    if (!clip.id) return;
    if (!$selection.includes(clip.id)) { selection.set([clip.id]); anchor = clip.id; }
    focused = clip.id;
    const rect = (event.currentTarget as HTMLElement | null)?.getBoundingClientRect();
    const pointer = event instanceof MouseEvent && event.type === "contextmenu";
    menu = { clip, x: pointer ? event.clientX : rect?.left ?? 20, y: pointer ? event.clientY : rect?.bottom ?? 20 };
  }
  async function contextAction(action: SelectionAction, target?: SendTarget): Promise<void> {
    const clip = menu?.clip;
    const mounted = selectedMounts;
    menu = null;
    if (!clip?.id) return;
    if (action === "remove") { await removeSelected(); return; }
    if (action === "trash") { profileAction = null; trashing = [...selectedReady]; return; }
    if (action === "send") sendClips([...selectedRaw], target);
    if (action === "profile-export" || action === "profile-apply" || action === "profile-clear") {
      profileAction = { action, chosen: selectedReady.filter(clip => clip.kind === "raw"), source: profileSource };
    }
    if (action === "folder" || action === "mount-folder") {
      const folders = [...new Set(action === "mount-folder" ? mounted.map(record => record.path)
        : selectedReady.flatMap(clip => [clip.path, ...clip.paths]).map(path => path.slice(0, path.lastIndexOf("/")) || "/"))];
      const failed: string[] = [];
      for (const folder of folders) {
        try { await $bridge?.api.open_folder(folder); }
        catch { failed.push(folder); }
      }
      problem = failed.length ? { ok: false, code: "open-folder-failed", message: `Could not open: ${failed.join(", ")}` } : null;
    }
    if (action === "mount") await mountClips(selectedReady);
    if (action === "unmount") await mountAction(mounted, "unmount");
  }

  // Names and the clip's own details (title, notes, keywords and so on), as last saved or being edited.
  function matchesSearch(clip: ClipSummary, query: string): boolean {
    const words = clip.id ? $annotationTexts[clip.id] ?? clip.details_text ?? "" : "";
    return clip.name.toLowerCase().includes(query) || words.toLowerCase().includes(query);
  }
  const byName = (a: ClipSummary, b: ClipSummary): number => a.name.localeCompare(b.name);
  const byCaptured = (a: ClipSummary, b: ClipSummary): number => (Date.parse(b.captured ?? "") || 0) - (Date.parse(a.captured ?? "") || 0) || byName(a, b);
  let shown = $derived.by(() => {
    const query = search.trim().toLowerCase();
    const list = (items ?? []).filter((clip) =>
      (filter === "all" || clip.kind === filter) && (query === "" || matchesSearch(clip, query)) && matchesFacets(clip, facets, filterNow));
    // Newest first; a clip without a capture time (copying, unreadable) goes last.
    return list.sort(byCaptured);
  });
  let selectedRaw = $derived((items ?? []).filter((clip) => clip.id !== null && clip.kind === "raw" && clip.status === "ready" && $selection.includes(clip.id)).map((clip) => clip.id as string));
  let selectedReady = $derived((items ?? []).filter(clip => clip.id !== null && clip.status === "ready" && $selection.includes(clip.id)));
  let selectedMounts = $derived([...new Map(selectedReady.flatMap(clip => clipMounts(clip).filter(mount => mount.active)).map(mount => [mount.id, mount])).values()]);
  let profileSource = $derived(selectedReady.find(clip => clip.id === focused && clip.kind === "raw") ?? selectedReady.find(clip => clip.kind === "raw") ?? null);
  let hiddenSelected = $derived(selectedReady.filter(clip => !shown.includes(clip)).length);
  let visibleReady = $derived(shown.filter(clip => clip.id !== null && clip.status === "ready"));
  let selectedVisible = $derived(visibleReady.filter(clip => $selection.includes(clip.id!)).length);
  function selectVisible(checked: boolean): void {
    const visible = new Set(visibleReady.map(clip => clip.id!));
    selection.set(checked ? [...new Set([...$selection, ...visible])] : $selection.filter(id => !visible.has(id)));
    focused = $selection.includes(focused ?? "") ? focused : ($selection.at(-1) ?? null);
  }
  let previewClips = $derived([...selectedReady].sort(byCaptured));
  let details = $derived(previewClips.find(clip => clip.id === focused) ?? previewClips[0] ?? null);
  let previewPosition = $derived(previewClips.findIndex(clip => clip.id === details?.id));
  async function selectPreview(id: string): Promise<void> {
    const current = details?.id;
    if (!current || !await saveDevelop() || !await annotationsFor(current).flush() || details?.id !== current) return;
    focused = id; warmPreview(id);
  }
  async function cyclePreview(direction: number): Promise<void> {
    const current = details?.id;
    const next = previewClips[previewPosition + direction];
    if (current && next?.id) await selectPreview(next.id);
  }

  function choose(clip: ClipSummary, event: MouseEvent | KeyboardEvent): void {
    const id = clip.id;
    if (id === null) return;
    if (clip.status === "ready" && focused !== id) warmPreview(id);
    const ids = shown.map((item) => item.id).filter((item): item is string => item !== null);
    if (event.shiftKey && anchor !== null && ids.includes(anchor)) {
      const [from, to] = [ids.indexOf(anchor), ids.indexOf(id)].sort((a, b) => a - b) as [number, number];
      selection.set(ids.slice(from, to + 1));
    } else if (event.ctrlKey || event.metaKey) {
      const chosen = new Set($selection);
      if (chosen.has(id)) chosen.delete(id);
      else chosen.add(id);
      selection.set([...chosen]);
      anchor = id;
    } else {
      selection.set([id]);
      anchor = id;
    }
    focused = $selection.includes(id) ? id : ($selection.at(-1) ?? null);
  }

  async function addFolder(folder: string): Promise<void> {
    if (readingFolder) return;
    readingFolder = folder;
    try {
      let current = $settings;
      if (current === null) {
        const loaded = await api.settings();
        if (!loaded.ok) { problem = loaded; return; }
        current = loaded.data;
      }
      const { problem: _, ...values } = current; // read-only: the engine refuses it in a PUT
      const result = await api.saveSettings({ ...values, folders: [...(current.folders ?? []), folder] });
      if (!result.ok) { problem = result; return; }
      problem = null;
      newFolder = "";
      settings.set(result.data);
      await loadLibrary();
    } finally { readingFolder = ""; }
  }

  async function chooseFolder(): Promise<void> {
    let folder: string | null = null;
    try {
      folder = (await $bridge?.api.choose_folder?.()) ?? null;
    } catch {
      problem = { ok: false, code: "choose-folder-failed", message: "The folder dialog could not be opened" };
      return;
    }
    if (folder !== null && folder !== "") await addFolder(folder);
  }

  function submitFolder(event: SubmitEvent): void {
    event.preventDefault();
    if (newFolder.trim() !== "") void addFolder(newFolder.trim());
  }

  onMount(() => {
    restoreSendHistory();
    void loadLibrary();
    if (view === "opened") void loadOpened();
    void loadMounts();
    const rateSelection = (event: KeyboardEvent) => {
      if (!/^[0-5]$/.test(event.key) || event.ctrlKey || event.metaKey || event.altKey || (event.target instanceof HTMLElement && event.target.closest("input, textarea, select, button, [contenteditable='true']"))) return;
      if (view && selectedReady.length) { event.preventDefault(); for (const clip of selectedReady) void annotationsFor(clip.id!).rate(Number(event.key)); }
    };
    window.addEventListener("keydown", rateSelection);
    const dropped = (event: Event) => {
      resetDrag();
      if (view === "library") { openProblem = { ok: false, code: "input-unreadable", message: "Choose Open clips to drop temporary files. Your library was not changed." }; return; }
      const paths = (event as CustomEvent<unknown>).detail;
      if (Array.isArray(paths) && paths.every(path => typeof path === "string")) void openFiles(paths);
    };
    window.addEventListener("mcrawsome-files-dropped", dropped);
    const enter = (event: DragEvent) => {
      if (view === "library" || !fileDrag(event)) return;
      event.preventDefault(); dragDepth += 1; draggingFiles = true; menu = null;
    };
    const over = (event: DragEvent) => {
      if (view === "library" || !fileDrag(event)) return;
      event.preventDefault(); draggingFiles = true;
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    };
    const leave = () => {
      if (!draggingFiles) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) resetDrag();
    };
    const droppedFiles = (event: DragEvent) => {
      const accept = view !== "library" && (draggingFiles || fileDrag(event));
      resetDrag();
      if (accept) drop(event);
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragover", over);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", droppedFiles);
    window.addEventListener("dragend", resetDrag);
    window.addEventListener("blur", resetDrag);
    const timer = window.setInterval(() => {
      filterNow = new Date();
      if (!document.hidden) { void loadMounts(); if (view === "opened" && !opening) void loadOpened(); }
    }, 5000);
    return () => {
      window.clearInterval(timer); window.removeEventListener("mcrawsome-files-dropped", dropped); window.removeEventListener("keydown", rateSelection);
      window.removeEventListener("dragenter", enter); window.removeEventListener("dragover", over);
      window.removeEventListener("dragleave", leave); window.removeEventListener("drop", droppedFiles);
      window.removeEventListener("dragend", resetDrag); window.removeEventListener("blur", resetDrag);
    };
  });
</script>

{#snippet openControls()}
  {#if $bridge?.api.choose_files}<button class="secondary" disabled={opening} onclick={() => void chooseFiles()}>Choose files</button>
  {:else}<form class="file-paths" onsubmit={(event) => { event.preventDefault(); void openFiles(filePaths.split(/\r?\n/).map(path => path.trim()).filter(Boolean)); }}><textarea aria-label="Clip file paths" placeholder="Full paths on this computer, one per line" bind:value={filePaths}></textarea><button disabled={opening || !filePaths.trim()}>Open files</button></form>{/if}
  {#if (items ?? []).length}<button class="quiet" title="Remove references from this view. Original files and mounted media are kept." disabled={opening} onclick={() => void clearOpened()}>Clear list</button>{/if}
  {#if opening}<p role="status">Reading clip references...</p>{/if}
{/snippet}

<section class="screen" aria-label={view === "opened" ? "Open clips" : undefined} ondrop={(event) => { if (view !== "library") { event.stopPropagation(); resetDrag(); drop(event); } }}>
  <header>
    <h1>{view === "opened" ? "Open clips" : "Library"}</h1>
    <div class="view-switch" role="group" aria-label="Clip source">
      <button class="quiet" aria-pressed={view === "library"} onclick={() => changeView("library")}>Library folders</button>
      <button class="quiet" aria-pressed={view === "opened"} onclick={() => changeView("opened")}>Open clips</button>
    </div>
    <SessionBar {workspace} onrestore={restoreSession} beforechange={beforeSessionChange} shoot={facets.shoot ? { label: facets.shoot, paths: shown.map(clip => clip.path) } : null} />
    <button class="secondary unmount-all" disabled={mountBusy || !mounts?.mounts?.some(mount => mount.active)} onclick={() => void unmountAll()}>Unmount All</button>
    {#if view === "library"}<FolderBar folders={$settings?.folders ?? []} missing={$missingFolders} />{/if}
  </header>
  {#each unmountResults.filter(result => !result.ok) as result (result.id)}<p role="alert">{result.source}: {result.message}</p>{/each}
  {#if $libraryProblem && view === "library"}<ErrorNote error={$libraryProblem} />{/if}
  {#if problem}<ErrorNote error={problem} />{/if}
  {#if openProblem}<ErrorNote error={openProblem} />{/if}
  {#if readingFolder}<p role="status">Reading the folder... {readingFolder}</p>{/if}

  {#if draggingFiles && view !== "library"}
    <div class="drop-stage drag-stage">
      <section class="drop-area" aria-label="Drop clips here">
        <svg class="drop-icon" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="5" y="9" width="30" height="28" rx="3" /><path d="m16 17 10 6-10 6Zm21 12v14m-7-7h14" /></svg>
        <div><h2>Drop clips here</h2><p>Files stay where they are and are not added to your library. No upload or copying.</p></div>
      </section>
    </div>
  {/if}
  <div class="stage" hidden={draggingFiles && view !== "library"}>
  {#if view !== "library"}
    {#if (items ?? []).length === 0}
      <div class="drop-stage">
        <section class="drop-area" aria-label="Drop clips here">
          <svg class="drop-icon" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="5" y="9" width="30" height="28" rx="3" /><path d="m16 17 10 6-10 6Zm21 12v14m-7-7h14" /></svg>
          <div><h2>Drop clips here</h2><p>Files stay where they are and are not added to your library. No upload or copying.</p></div>
          {@render openControls()}
        </section>
      </div>
    {:else}
      <div class="open-toolbar"><p>Drop clips here to add more</p>{@render openControls()}</div>
    {/if}
  {/if}
  {#if view === "library" && items !== null && items.length === 0}
    <div class="empty">
      <h2>Add a folder of MotionCam clips</h2>
      {#if $bridge?.api.choose_folder}
        <button onclick={() => void chooseFolder()}>Choose a folder</button>
      {:else}
        <form onsubmit={submitFolder}>
          <input type="text" aria-label="Folder path" placeholder="/path/to/clips" bind:value={newFolder} spellcheck="false" />
          <button type="submit">Add folder</button>
        </form>
      {/if}
    </div>
  {:else if items !== null && items.length > 0}
    <div class="content" class:has-selection={!!details} class:docked={!!details && $previewDocked}>
      <div class="library-pane">
        <div class="tools">
          <div class="filters" role="group" aria-label="Show">
            {#each FILTERS as option (option.value)}
              <button class="quiet" aria-pressed={filter === option.value} onclick={() => filter = option.value}>{option.label}</button>
            {/each}
          </div>
          <button class="filter-button quiet" aria-expanded={filtersOpen} aria-controls="library-filters" onclick={() => filtersOpen = !filtersOpen}><Icon name="filter" />Filter{Object.values(facets).some(Boolean) ? " (active)" : ""}</button>
          <input type="search" aria-label="Search names and details" placeholder="Search names and details" title="Matches file names and each clip's title, description, keywords, notes, scene, shot, take, location and camera label" bind:value={search} spellcheck="false" />
        </div>
        {#if filtersOpen}<div class="detailed-filters" id="library-filters">
          <div class="facet-controls">
            {#each FACETS as [key, label]}<label>{label}<select aria-label={`Filter ${label}`} value={facets[key] ?? ""} onchange={(event) => { filterNow = new Date(); facets = { ...facets, [key]: event.currentTarget.value }; }}>
              <option value="">Any</option>
              {#each [...new Set([...facetValues(items ?? [], key), ...(facets[key] ? [facets[key]!] : [])])] as value}<option value={value}>{facetLabel(key, value)}</option>{/each}
            </select></label>{/each}
            <button class="quiet" onclick={() => facets = {}}>Clear filters</button>
          </div>
        </div>{/if}
        <div class="bulk-tools" role="toolbar" aria-label="Selected clips">
          <input class="clip-selection" type="checkbox" aria-label="Select clips in this view" title="Select or deselect the visible clips" checked={visibleReady.length > 0 && selectedVisible === visibleReady.length} indeterminate={selectedVisible > 0 && selectedVisible < visibleReady.length} disabled={visibleReady.length === 0} onchange={(event) => selectVisible(event.currentTarget.checked)} />
          <strong>{selectedReady.length} selected</strong>{#if hiddenSelected}<span class="muted">{hiddenSelected} outside this view</span>{/if}
          <ClipLists ids={selectedReady.map(clip => clip.id!)} positions={selectedReady.filter(clip => clip.position).length} />
        </div>
        {#if $sendActivity?.entries.some(entry => (items ?? []).some(clip => clip.id === entry.id))}<SendActivity />{/if}
        {#if profileAction}
          <VignetteActions {...profileAction} onclose={() => profileAction = null} onchanged={() => { discardWarmPreview(); profileRevision += 1; void loadLibrary(); }} />
        {/if}
        {#if rateCheck}
          <TimelineRates clips={rateCheck.clips} onclose={() => rateCheck = null} onfix={useWholeRates} />
        {/if}
        {#if trashing}
          <TrashConfirm chosen={trashing} onclose={() => trashing = null} onchanged={() => { discardWarmPreview(); selection.set([]); focused = null; void loadMounts(); void loadLibrary(); }} />
        {/if}
        <div class="clips">
          {#if shown.length > 0}
            <ClipGrid contextId={menu?.clip.id ?? null} active={details?.id ?? null} clips={shown} {clipMounts} {isMounting} prefetch={details === null} selected={$selection} onchoose={choose} oncontext={context} />
          {:else}
            <p class="muted">No clips match.</p>
          {/if}
        </div>
      </div>
      {#if details}
        <section class="right-pane" aria-label="Clip preview and information">
          {#if previewClips.length > 1}<nav class="preview-nav" aria-label="Selected clip preview">
            <button class="secondary" aria-label="Previous selected clip" disabled={previewPosition <= 0} onclick={() => void cyclePreview(-1)}>‹</button>
            <div><span aria-live="polite">{previewPosition + 1} of {previewClips.length} selected</span><strong title={details.path}>{details.name}</strong></div>
            <button class="secondary" aria-label="Next selected clip" disabled={previewPosition >= previewClips.length - 1} onclick={() => void cyclePreview(1)}>›</button>
          </nav>{/if}
          {#key `${details.id}:${profileRevision}`}{#if details.id}
            <Develop id={details.id} docked={$previewDocked} ondock={toggleDock} onflush={(flush) => flushDevelop = flush} onplayback={(value) => previewPlaying = value}>
              {#snippet inspector(develop, savingText)}
                <ClipInspector clip={details} bind:tab={inspectorTab} {develop} {savingText} playing={previewPlaying} mounts={clipMounts(details)} {mountBusy} mountAvailable={mounts?.available !== false} mountReason={mounts?.reason ?? ""}
                  onmount={() => void mountClips([details])}
                  onmountaction={(record, action) => void mountAction([record], action)}
                  onnext={previewPosition < previewClips.length - 1 ? () => void cyclePreview(1) : undefined} />
              {/snippet}
            </Develop>
          {/if}{/key}
        </section>
      {:else}<div class="preview-empty">Select a clip to preview it and view its metadata.</div>{/if}
    </div>
  {/if}
  </div>
  {#if menu}<ClipContextMenu canRemove={view !== "library"} x={menu.x} y={menu.y} raw={selectedRaw.length > 0} count={selectedReady.length} sourceName={profileSource?.name ?? ""} busy={opening || mountBusy || profileAction !== null || trashing !== null} mounted={selectedMounts.length > 0} mountAvailable={mounts?.available !== false} inUse={selectedMounts.some(mount => mount.in_use === true)} canOpenFolder={$bridge?.api.open_folder !== undefined} onaction={(action, target) => void contextAction(action, target)} onclose={() => menu = null} />{/if}
</section>

<style>
  .filter-button { display: inline-flex; align-items: center; gap: 5px; }
  .filter-button[aria-expanded="true"] { color: var(--accent); }
  .detailed-filters { margin-bottom: 12px; font-size: 12px; }
  .unmount-all { margin-left: auto; font-size: 12px; }
  .facet-controls { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; padding-top: 12px; }
  .facet-controls label { display: grid; gap: 4px; }
  .view-switch { display: flex; gap: 2px; padding: 3px; background: var(--panel); border-radius: 6px; }
  .view-switch button[aria-pressed="true"] { background: var(--card); color: var(--text); }
  .drop-stage { min-height: calc(100dvh - 180px); display: grid; grid-template-columns: minmax(0, 1.12fr) minmax(0, 1fr); gap: 16px; align-items: center; }
  .drop-area { box-sizing: border-box; width: min(100%, calc(100dvh - 240px)); aspect-ratio: 1; justify-self: center; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 24px; padding: 32px; border: 1px dashed var(--accent); border-radius: var(--radius); background: var(--panel); text-align: center; }
  .drop-icon { width: 48px; height: 48px; color: var(--accent); }
  .drop-area h2 { margin: 0 0 12px; font-size: 20px; }
  .drop-area p { margin: 0; max-width: 320px; color: var(--muted); font-size: 12px; line-height: 1.6; }
  .open-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-bottom: 16px; }
  .open-toolbar p { margin: 0; flex: 1; color: var(--muted); font-size: 12px; }
  .file-paths { max-width: 100%; flex-wrap: wrap; justify-content: center; }
  .file-paths textarea { box-sizing: border-box; width: 240px; max-width: 100%; font: inherit; font-size: 12px; }
  .right-pane { display: flex; flex-direction: column; min-height: 0; overflow: hidden; min-width: 0; }
  /* Stretch across the pane: sized to its contents, the preview grew whenever a status line was longer.
     The settings scroll within the inspector, so the picture never moves. */
  .right-pane :global(.develop) { position: static; max-height: none; min-height: 0; overflow: hidden; flex: 1; align-self: stretch; }
  .preview-nav { flex: none; display: flex; align-items: center; gap: 12px; padding: 8px 12px; margin-bottom: 12px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); }
  .preview-nav div { display: grid; gap: 3px; min-width: 0; flex: 1; }
  .preview-nav span { color: var(--muted); font-size: 11px; }
  .preview-nav strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; font-weight: 500; }
  .preview-nav button { font-size: 18px; padding: 4px 10px; }
  .bulk-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 9px 12px; margin-bottom: 10px; border-radius: 6px; background: var(--panel); }
  .bulk-tools strong { font-size: 12px; margin-right: 4px; }
  .preview-empty { display: grid; place-items: center; min-height: 360px; padding: 32px; color: var(--muted); background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); }
  /* The screen fills the window, so the clip list and the preview pane end at the same bottom edge and
     scroll within themselves. */
  .screen {
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    height: 100%;
    padding: 18px;
  }
  .drag-stage { pointer-events: none; }
  .stage:not([hidden]) { display: flex; flex-direction: column; flex: 1; min-height: 0; }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 16px;
    margin-bottom: 16px;
  }
  h1 {
    margin: 0;
    font-size: 20px;
  }
  .tools {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
    margin-bottom: 10px;
    padding: 8px 10px;
    background: var(--panel);
    border-radius: 6px;
  }
  .filters {
    display: flex;
    gap: 2px;
    padding: 2px;
    border: 1px solid var(--line);
    border-radius: 8px;
  }
  .filters button[aria-pressed="true"] {
    background: var(--card);
    color: var(--text);
  }

  input[type="search"] {
    flex: 0 1 240px;
    min-width: 0;
    margin-left: auto;
    padding: 6px 8px;
    border: 1px solid var(--line);
    border-radius: 6px;
    background: var(--panel);
  }
  .content {
    display: grid;
    grid-template-columns: minmax(0, 1.12fr) minmax(0, 1fr);
    grid-template-rows: minmax(0, 1fr);
    flex: 1;
    min-height: 0;
    gap: 16px;
  }
  /* The preview pane keeps its own width (about 40em, at most 45% of the window), whatever it shows;
     the cards take the rest. */
  .content.has-selection { grid-template-columns: minmax(0, 1fr) clamp(30em, 40em, 45%); }
  /* Docked, the pane spans both columns: its picture where the cards were, its settings beside it. */
  .content.docked { grid-template-columns: minmax(0, 1fr); }
  .content.docked .library-pane { display: none; }
  .library-pane { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  .clips {
    flex: 1;
    min-width: 0;
    min-height: 0;
    overflow-y: auto;
  }
  .empty {
    max-width: 520px;
    margin: 48px auto;
    padding: 24px;
    border: 1px dashed var(--line);
    border-radius: var(--radius);
    text-align: center;
  }
  .empty h2 {
    margin: 0 0 16px;
    font-size: 16px;
    font-weight: 600;
  }
  form {
    display: flex;
    gap: 8px;
  }
  form input {
    flex: 1;
  }
  .muted {
    color: var(--muted);
  }
  @media (max-width: 900px) {
    .content, .content.has-selection {
      grid-template-columns: minmax(0, 1fr);
    }
    .screen, .stage:not([hidden]) { display: block; height: auto; }
    .content { flex: none; grid-template-rows: none; }
    .clips { flex: none; max-height: 40vh; }
    .right-pane { overflow: visible; }
    .right-pane :global(.develop) { overflow: visible; flex: none; }
  }
</style>
