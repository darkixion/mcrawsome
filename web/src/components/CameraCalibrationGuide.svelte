<script lang="ts">
  import { onMount } from "svelte";
  import { bridge } from "../lib/bridge";
  import { CAPTURE_STEPS, TARGET, remainingSeconds } from "../lib/camera-guide";

  let { onclose }: { onclose: (completed: boolean, log: Record<string, unknown>) => void } = $props();
  const identity = $props.id();
  const id = `camera-target-${identity}`;
  let dialog: HTMLDialogElement;
  let viewport: HTMLDivElement;
  let index = $state(0);
  let elapsed = $state(0);
  let running = $state(false);
  let started = $state(false);
  let completed = $state(false);
  let voice = $state(false);
  let message = $state("");
  let fullscreen = $state(false);
  let step = $derived(CAPTURE_STEPS[index]!);
  let remaining = $derived(remainingSeconds(step, elapsed));
  const history: { step: string; distance: string; at: string; event: string }[] = [];
  type FullDocument = Document & { webkitFullscreenElement?: Element; webkitExitFullscreen?: () => void };
  type FullElement = HTMLDivElement & { webkitRequestFullscreen?: () => void };
  const current = () => document.fullscreenElement ?? (document as FullDocument).webkitFullscreenElement;
  let previous = 0;

  function record(event: string): void {
    history.push({ step: step.title, distance: step.distance, at: new Date().toISOString(), event });
  }
  function speak(text = step.gate ? `${step.title}. ${step.instruction}` : `${step.title}. ${step.seconds} seconds.`): void {
    if (!voice || !("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  }
  function pause(reason = "Paused. Keep the phone steady; resume when ready."): void {
    if (running) record("pause");
    running = false; message = reason;
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  }
  function start(): void {
    previous = performance.now(); running = true; started = true; message = ""; record("start"); speak();
  }
  function repeat(): void { elapsed = 0; start(); }
  function finish(): void {
    onclose(completed, { protocol_version: 1, completed, target: TARGET, events: history,
      display: { width_css_px: window.innerWidth, height_css_px: window.innerHeight, device_pixel_ratio: window.devicePixelRatio },
      note: "Display event times are not synchronised to the recording and are not measurement evidence." });
  }
  async function enterFullscreen(): Promise<void> {
    pause("Line up the screen again after changing display size.");
    try {
      try {
        if (viewport.requestFullscreen) await viewport.requestFullscreen();
        else if ((viewport as FullElement).webkitRequestFullscreen) (viewport as FullElement).webkitRequestFullscreen!();
        else throw new Error("Fullscreen unavailable");
      } catch (error) {
        if (!$bridge?.api.request_fullscreen || !await $bridge.api.request_fullscreen(id)) throw error;
      }
      message = "Full screen ready. Line up the display, then start or resume.";
    } catch { message = "Full screen is unavailable in this window. Maximise the window and line up the whole display before starting."; }
  }

  onMount(() => {
    dialog.showModal();
    const changed = () => {
      const active = current() === viewport;
      if (fullscreen && !active) pause("Full screen ended. Realign the display before resuming.");
      fullscreen = active;
    };
    const visibility = () => { if (document.hidden) pause("The guide was hidden. Realign the phone before resuming."); };
    const resized = () => { if (running) pause("Display size changed. Realign the whole screen before resuming."); };
    const interval = window.setInterval(() => {
      const now = performance.now();
      if (running) {
        elapsed += (now - previous) / 1000;
        if (elapsed >= step.seconds) {
          record("complete"); elapsed = 0;
          if (index === CAPTURE_STEPS.length - 1) { completed = true; running = false; speak("Capture complete. Stop recording."); }
          else {
            index += 1;
            if (step.gate) { running = false; message = "Take your time. Start the farther pass when you are ready."; }
            speak();
          }
        }
      }
      previous = now;
    }, 100);
    document.addEventListener("fullscreenchange", changed);
    document.addEventListener("webkitfullscreenchange", changed);
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("resize", resized);
    return () => {
      clearInterval(interval);
      if ("speechSynthesis" in window) window.speechSynthesis.cancel();
      if (current() === viewport) {
        if (document.exitFullscreen) void document.exitFullscreen().catch(() => {});
        else (document as FullDocument).webkitExitFullscreen?.();
      }
      document.removeEventListener("fullscreenchange", changed);
      document.removeEventListener("webkitfullscreenchange", changed);
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("resize", resized);
      dialog.close();
    };
  });
</script>

<dialog bind:this={dialog!} aria-label="Camera calibration target" oncancel={(event) => { event.preventDefault(); pause("Paused. Use Close guide to leave, or resume when ready."); }}>
  <div {id} bind:this={viewport!} class="guide" data-mcrawsome-viewport="true">
  <div class="target-area">
    <svg class="target" viewBox="0 0 12 9" role="img" aria-label="Calibration checkerboard, 10 columns by 7 rows of square cells" shape-rendering="crispEdges">
      <rect width="12" height="9" fill="white" />
      {#each Array.from({ length: TARGET.rows * TARGET.columns }, (_, cell) => cell) as cell}
        {#if (Math.floor(cell / TARGET.columns) + cell % TARGET.columns) % 2 === 0}
          <rect x={1 + cell % TARGET.columns} y={1 + Math.floor(cell / TARGET.columns)} width="1" height="1" fill="black" />
        {/if}
      {/each}
    </svg>
  </div>
  <div class="instructions">
    <div class="statusline"><span>CAMERA CALIBRATION · {step.distance === "near" ? "CLOSE PASS" : "FARTHER PASS"}</span><span>Step {index + 1} of {CAPTURE_STEPS.length}</span></div>
    <div class="prompt">
      <div class="cue" aria-hidden="true">{completed ? "✓" : step.cue}</div>
      <div aria-live="polite" aria-atomic="true"><h2>{completed ? "Capture complete. Stop recording" : step.title}</h2><p>{completed ? "Transfer the original recording and its gyro file to this computer. Close the guide to analyse your recording and save its JSON profile." : step.instruction}</p></div>
      <div class="countdown" role="timer" aria-label="Seconds remaining">{completed ? "✓" : step.gate && !running ? "–" : remaining}<small>{completed ? "done" : "seconds"}</small></div>
    </div>
    <div class="controls">
      {#if completed}<button onclick={finish}>Analyse recording</button>
      {:else if running}<button onclick={() => pause()}>Pause</button>
      {:else}<button onclick={start}>{step.gate ? "Start farther pass" : started ? "Resume" : "Start countdown"}</button>{/if}
      {#if started && !completed}<button class="secondary" onclick={repeat}>Repeat this step</button>{/if}
      {#if !fullscreen}<button class="secondary" onclick={() => void enterFullscreen()}>Use full screen</button>{/if}
      <label><input type="checkbox" bind:checked={voice} />Spoken prompts</label>
      <button class="secondary close" onclick={finish}>Close guide</button>
    </div>
    <p class="message" role="status">{message || "Keep recording throughout both passes. The target stays fixed; only move the phone."}</p>
  </div>
  </div>
</dialog>

<style>
  dialog { position: fixed; inset: 0; width: 100vw; height: 100dvh; max-width: none; max-height: none; padding: 0; margin: 0; border: 0; background: white; color: #f4f5f7; overflow: hidden; }
  dialog[open] { display: flex; flex-direction: column; }
  .guide { display: flex; flex-direction: column; width: 100%; height: 100%; }
  .guide:fullscreen { width: 100vw; height: 100dvh; background: white; }
  dialog::backdrop { background: #000; }
  .target-area { flex: 1; min-height: 0; display: flex; align-items: center; justify-content: center; padding: 1vh 2vw; background: white; }
  .target { display: block; height: 100%; width: 100%; max-width: 100%; object-fit: contain; }
  .instructions { flex: 0 0 245px; box-sizing: border-box; background: #101722; padding: 16px clamp(16px, 4vw, 64px); }
  .statusline { display: flex; justify-content: space-between; gap: 12px; color: #aab7ca; font: 11px var(--mono); letter-spacing: .08em; }
  .prompt { display: flex; gap: 22px; align-items: center; min-height: 118px; }
  .cue { width: 65px; flex-shrink: 0; text-align: center; font-size: 56px; color: #a6d5ff; }
  h2 { margin: 8px 0; font-size: clamp(18px, 2.4vw, 28px); color: white; }
  p { margin: 0; font-size: 14px; line-height: 1.5; max-width: 880px; }
  .countdown { margin-left: auto; width: 86px; flex-shrink: 0; text-align: center; font: 56px var(--mono); font-variant-numeric: tabular-nums; }
  .countdown small { display: block; font: 11px var(--mono); color: #aab7ca; }
  .controls { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
  .controls label { display: flex; align-items: center; gap: 6px; font-size: 12px; }
  button.secondary { background: #202d40; color: #f4f5f7; border-color: #42546e; }
  button.close { margin-left: auto; }
  .message { margin-top: 9px; font-size: 11px; color: #bdc9db; }
  @media (max-height: 650px), (max-width: 650px) { .instructions { flex-basis: 230px; padding: 10px 16px; } .cue { display: none; } .prompt { gap: 8px; min-height: 116px; } p { font-size: 12px; } .countdown { width: 60px; font-size: 40px; } button { font-size: 11px; padding: 6px 8px; } .controls { gap: 6px; } }
</style>
