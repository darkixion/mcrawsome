<script lang="ts">
  import { onDestroy, onMount, tick as renderTick, type Snippet } from "svelte";
  import { formatClock } from "../lib/format";
  import { settings } from "../lib/settings";
  import { bridge } from "../lib/bridge";
  import TrimBar from "./TrimBar.svelte";
  import Icon from "./Icon.svelte";
  let { menu, compact = false, showTimeline = true, preloadAudio = false, id, title = null, index, count, rate, start, end, reached, onseek, ontrim = () => {}, onplaying, onscrub = onseek, onrest = () => {}, onclock = () => {}, onprepare = () => {} }: {
    menu?: Snippet;
    compact?: boolean; showTimeline?: boolean; preloadAudio?: boolean; id: string; title?: string | null; index: number; count: number; rate: number; start: number; end: number; reached: number;
    onseek: (index: number) => void; ontrim?: (start: number, end: number) => void; onplaying: (playing: boolean) => void; onclock?: (index: number) => void;
    onprepare?: (index: number | null) => void;
    onscrub?: (index: number) => void; onrest?: () => void;
  } = $props();
  let controls: HTMLDivElement;
  let audio = $state<HTMLAudioElement>();
  let audioRequested = $state(false);
  let starting = $state(false);
  let playbackRevision = 0;
  let disposed = false;
  let mediaCheck: Promise<boolean> | null = null;
  let bridgeReady: (() => void) | null = null;
  let audioClock = false;
  let clockTime = 0;
  let clockStarted = 0;
  let playing = $state(false);
  let problem = $state("");
  let volume = $state(1);
  let muted = $state(false);
  let silenced = $derived(muted || volume === 0);
  // The desktop's media keys and media controls follow this player, through the window (MPRIS).
  $effect(() => { void $bridge?.api.media_state?.(playing || starting, title, id); });
  onDestroy(() => { void $bridge?.api.media_state?.(false, null, null); });
  $effect(() => {
    const key = (event: Event): void => {
      const action = (event as CustomEvent<unknown>).detail;
      if (action === "play-pause" || (action === "play" && !playing && !starting)) void play();
      else if ((action === "pause" || action === "stop") && (playing || starting)) pause();
    };
    window.addEventListener("mcrawsome-media-key", key);
    return () => window.removeEventListener("mcrawsome-media-key", key);
  });
  let replay = $derived(!playing && !starting && count > 1 && index >= end - 1);
  function toggleMute() {
    if (silenced) { muted = false; if (volume === 0) volume = 0.5; }
    else muted = true;
  }
  let tick = 0;
  let last = -1;
  async function mediaAvailable(): Promise<boolean> {
    if (disposed) return false;
    if (!$bridge && navigator.userAgent.includes("mcrawsome-pywebview")) {
      await new Promise<void>(resolve => {
        const ready = () => {
          window.removeEventListener("pywebviewready", ready);
          bridgeReady = null;
          resolve();
        };
        bridgeReady = ready;
        window.addEventListener("pywebviewready", ready, { once: true });
        if (window.pywebview) ready();
      });
    }
    return !disposed && await ($bridge?.api.media_available?.() ?? true);
  }
  async function prepareAudio(): Promise<boolean> {
    // Native media must pass its runtime check before WebKit sees an audio element. Preparing the
    // selected clip performs that check and fetches metadata without starting playback.
    mediaCheck ??= Promise.resolve().then(mediaAvailable);
    if (!await mediaCheck || disposed || (!preloadAudio && !starting)) return false;
    audioRequested = true;
    await renderTick();
    return !!audio;
  }
  onMount(() => { if (preloadAudio) void prepareAudio().catch(() => { mediaCheck = null; }); });
  export function pause() {
    ++playbackRevision;
    const wasPlaying = playing;
    const wasStarting = starting;
    playing = false; starting = false; audioClock = false; audio?.pause(); cancelAnimationFrame(tick);
    if (wasStarting) onprepare(null);
    if (wasPlaying) onplaying(false);
  }
  function playbackTime(): number {
    const now = performance.now();
    if (audioClock && audio && !audio.seeking) {
      clockTime = audio.currentTime;
      clockStarted = now;
    }
    return clockTime + (now - clockStarted) / 1000;
  }
  function advance() {
    if (!playing) return;
    const next = Math.floor(playbackTime() * rate);
    if (next >= end) {  // stop on the Out mark's frame, ready to replay from the In mark
      pause();
      if (last !== end - 1) onseek(Math.max(start, end - 1));
      return;
    }
    onclock(next);
    if (next !== last) { last = next; onseek(next); }
    tick = requestAnimationFrame(advance);
  }
  async function play() {
    if (playing || starting) { pause(); return; }
    starting = true;
    const asked = ++playbackRevision;
    const position = index >= end - 1 || index < start ? start : index;
    onprepare(position);
    // A picture does not need an audio device. Keep the transport moving while that device opens,
    // then seek late audio to the current picture before handing the clock over to it.
    clockTime = position / rate; clockStarted = performance.now(); audioClock = false;
    last = -1; playing = true; problem = ""; onplaying(true); advance();
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const expired = new Promise<never>((_, reject) => {
      timeout = setTimeout(() => reject(new Error("Audio timed out")), 5000);
    });
    try {
      const available = await Promise.race([prepareAudio(), expired]);
      if (asked !== playbackRevision) return;
      if (!available) { mediaCheck = null; problem = "Audio is unavailable. Video will continue without sound."; return; }
      if (!audio || asked !== playbackRevision) return;
      audio.currentTime = playbackTime();
      await Promise.race([audio.play(), expired]);
      if (asked !== playbackRevision) { if (!playing && !starting) audio.pause(); return; }
      const target = playbackTime();
      if (Math.abs(audio.currentTime - target) > 1 / rate) audio.currentTime = target;
      audioClock = true; problem = "";
    } catch {
      if (asked === playbackRevision) { audio?.pause(); audioClock = false; problem = "Audio could not play. Video will continue without sound."; }
    } finally {
      clearTimeout(timeout);
      if (asked === playbackRevision) { starting = false; if (!playing) onprepare(null); }
    }
  }
  function seek(next: number) { pause(); onseek(Math.max(0, Math.min(count - 1, next))); }
  function keyboard(event: KeyboardEvent): void {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (![" ", "ArrowLeft", "ArrowRight", "i", "o"].includes(event.key)) return;
    const target = event.target instanceof Element ? event.target : null;
    // Any video control (in the viewer or these controls) keeps Space and Left/Right for playback, even a
    // focused button or slider; an open menu keeps its arrows, and a trim handle's own arrows move its mark.
    if (!videoControl(target)) {
      if (target?.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="menu"], [role="slider"], summary')) return;
      if (target?.closest("button, a")) return;
    }
    if (count < 1 || rate <= 0) return;
    event.preventDefault();
    if (event.key === " ") { if (!event.repeat) void play(); return; }
    if (event.key === "i" || event.key === "o") {  // marks at the playhead, as the handles would
      if (!compact && !event.repeat) {
        if (event.key === "i") ontrim(Math.min(index, end - 1), end);
        else ontrim(start, Math.max(index + 1, start + 1));
      }
      return;
    }
    const direction = event.key === "ArrowLeft" ? -1 : 1;
    if (playing) {
      const next = Math.max(start, Math.min(end - 1, Math.floor(playbackTime() * rate) + Math.round(5 * rate) * direction));
      clockTime = next / rate; clockStarted = performance.now();
      if (audio) audio.currentTime = clockTime;
      last = next;
      onclock(next);
      onseek(next);
    } else seek(index + direction);
  }
  function videoControl(target: Element | null): boolean {
    if (!target || target.closest('[role="menu"]')) return false;
    return !!controls?.contains(target) || !!target.closest("[data-mcrawsome-viewport]");
  }
  function keyup(event: KeyboardEvent): void {  // a focused button would otherwise click on Space's release
    const target = event.target instanceof Element ? event.target : null;
    if (event.key === " " && target?.closest("button") && videoControl(target)) event.preventDefault();
  }
  onDestroy(() => { disposed = true; bridgeReady?.(); pause(); });
</script>
<svelte:window onkeydown={keyboard} onkeyup={keyup} />
{#if audioRequested}<audio bind:this={audio} bind:volume bind:muted src={`/api/v1/clips/${encodeURIComponent(id)}/audio`} preload={preloadAudio ? "metadata" : "none"} onended={pause} onerror={() => { audioClock = false; if (starting || playing) problem = "Audio is unavailable. Video will continue without sound."; }}></audio>{/if}
<div bind:this={controls} class="transport" role="group" aria-label="Video controls">
  <div class="stepping">
    <button class="control" aria-label="Skip to beginning" title={start > 0 ? "Skip to the In mark (again for the first frame)" : "Skip to beginning"} onclick={() => seek(index !== start ? start : 0)}><Icon name="start" /></button>
    <button class="control" aria-label="Previous frame" title="Previous frame (Left when paused)" onclick={() => seek(index - 1)}><Icon name="back" /></button>
    <button class="control play" aria-label={playing ? "Pause" : starting ? "Cancel" : replay ? "Replay" : "Play"} title={playing ? "Pause (Space)" : starting ? "Cancel playback startup (Space)" : replay ? "Replay from the In mark (Space)" : "Play (Space)"} onclick={play}><Icon name={playing || starting ? "pause" : replay ? "replay" : "play"} /></button>
    <button class="control" aria-label="Next frame" title="Next frame (Right when paused)" onclick={() => seek(index + 1)}><Icon name="forward" /></button>
    <button class="control" aria-label="Skip to end" title={end < count ? "Skip to the Out mark (again for the last frame)" : "Skip to end"} onclick={() => seek(index !== end - 1 ? end - 1 : count - 1)}><Icon name="end" /></button>
  </div>
  {#if showTimeline}<div class="timeline"><TrimBar {index} {count} {start} {end} {rate} showRange={!compact} onseek={(value) => { pause(); onscrub(value); }} {onrest} {ontrim} onpreview={(value) => { pause(); onscrub(value); }} /></div>{/if}
  <div class="position"><output aria-label="Frame position">{Math.min(count, index + 1)} / {count}</output><span class="clock">{formatClock(index / rate)} / {formatClock(count / rate)}</span>{#if $settings?.show_preview_stats && !compact && playing}<span class="clock" title="Frames shown per second during playback">{reached.toFixed(1)} fps</span>{/if}</div>
  {#if menu}{@render menu()}{/if}
  <div class="view-audio">
    <button class="control" aria-label={silenced ? "Unmute" : "Mute"} title={silenced ? "Unmute" : "Mute"} onclick={toggleMute}><Icon name={silenced ? "muted" : "volume"} /></button>
    <input class="volume" type="range" aria-label="Volume" title={`Volume ${Math.round(volume * 100)}%`} min="0" max="1" step="0.01" value={volume} oninput={(event) => { volume = Number(event.currentTarget.value); muted = false; }} />
  </div>
</div>
{#if problem}<p role="status">{problem}</p>{/if}
<style>
  .transport { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
  .stepping, .view-audio { display: flex; align-items: center; gap: 2px; }
  .view-audio { margin-left: auto; }
  .control { display: grid; place-items: center; min-width: 28px; height: 30px; padding: 4px; background: transparent; color: var(--text); border: 1px solid transparent; }
  .control:hover { background: var(--card); }
  .play { color: var(--bg); background: var(--text); border-radius: 6px; }
  .play:hover { background: var(--accent); }
  .timeline { flex: 1 1 120px; min-width: 100px; }
  .timeline :global(.bar) { margin: 0; }
  .position { display: grid; text-align: right; font-variant-numeric: tabular-nums; }
  output { font-size: 11px; white-space: nowrap; }
  .clock { font-size: 9px; white-space: nowrap; }
  .volume { width: 62px; accent-color: var(--accent); }
  p { color: var(--muted); font-size: 12px; }
</style>
