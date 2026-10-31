import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import Transport from "../src/components/Transport.svelte";

const props = { id: "a", index: 0, count: 10, rate: 24, start: 0, end: 10, reached: 0, onseek: vi.fn(), onplaying: vi.fn() };
beforeEach(() => { vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {}); });
afterEach(() => { cleanup(); delete window.pywebview; vi.restoreAllMocks(); });

it("opens without initialising a media backend", () => {
  const { container } = render(Transport, props);
  expect(container.querySelector("audio")).toBeNull();
});

it.each([true, false])("waits for a late desktop bridge before checking media support (%s)", async (available) => {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue("mcrawsome-pywebview");
  const view = render(Transport, { ...props, preloadAudio: true });
  await Promise.resolve(); await Promise.resolve();
  expect(view.container.querySelector("audio")).toBeNull();
  const check = vi.fn(async () => available);
  window.pywebview = { api: { choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn(), media_available: check } };
  window.dispatchEvent(new Event("pywebviewready"));
  await waitFor(() => expect(check).toHaveBeenCalledOnce());
  if (available) await waitFor(() => expect(view.container.querySelector("audio")).toBeInTheDocument());
  else expect(view.container.querySelector("audio")).toBeNull();
});

it("cancels waiting for the desktop bridge when the clip closes", async () => {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue("mcrawsome-pywebview");
  const view = render(Transport, { ...props, preloadAudio: true });
  await Promise.resolve(); await Promise.resolve();
  view.unmount();
  const check = vi.fn(async () => true);
  window.pywebview = { api: { choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn(), media_available: check } };
  window.dispatchEvent(new Event("pywebviewready"));
  await Promise.resolve(); await Promise.resolve();
  expect(check).not.toHaveBeenCalled();
  expect(view.container.querySelector("audio")).toBeNull();
});

it("prepares selected audio after the native check without playing, then reuses that check", async () => {
  const playing = vi.fn();
  let finishCheck: (value: boolean) => void = () => {};
  const check = vi.fn(() => new Promise<boolean>(resolve => { finishCheck = resolve; }));
  const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  window.pywebview = { api: { choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn(), media_available: check } };
  const view = render(Transport, { ...props, preloadAudio: true, onplaying: playing });
  await waitFor(() => expect(check).toHaveBeenCalledOnce());
  expect(view.container.querySelector("audio")).toBeNull();
  finishCheck(true);
  await waitFor(() => expect(view.container.querySelector("audio")).toHaveAttribute("preload", "metadata"));
  expect(play).not.toHaveBeenCalled();
  expect(playing).not.toHaveBeenCalledWith(true);
  await fireEvent.click(screen.getByRole("button", { name: "Play" }));
  await screen.findByRole("button", { name: "Pause" });
  expect(check).toHaveBeenCalledOnce();
});

it("does not create selected audio after the clip has closed during its native check", async () => {
  let finishCheck: (value: boolean) => void = () => {};
  const check = vi.fn(() => new Promise<boolean>(resolve => { finishCheck = resolve; }));
  window.pywebview = { api: { choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn(), media_available: check } };
  const view = render(Transport, { ...props, preloadAudio: true });
  await waitFor(() => expect(check).toHaveBeenCalledOnce());
  view.unmount(); finishCheck(true);
  await Promise.resolve(); await Promise.resolve();
  expect(view.container.querySelector("audio")).toBeNull();
});

it("offers the full viewport transport with frame positions and bounded end jumps", async () => {
  const seek = vi.fn();
  render(Transport, { ...props, index: 4, onseek: seek });
  expect(screen.getByLabelText("Frame position")).toHaveTextContent("5 / 10");
  for (const name of ["Skip to beginning", "Previous frame", "Play", "Next frame", "Skip to end", "Mute"]) {
    expect(screen.getByRole("button", { name })).toBeInTheDocument();
  }
  await fireEvent.click(screen.getByRole("button", { name: "Skip to beginning" }));
  expect(seek).toHaveBeenLastCalledWith(0);
  await fireEvent.click(screen.getByRole("button", { name: "Skip to end" }));
  expect(seek).toHaveBeenLastCalledWith(9);
});

it("keeps mute and volume choices before creating the audio backend", async () => {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  const { container } = render(Transport, props);
  await fireEvent.input(screen.getByRole("slider", { name: "Volume" }), { target: { value: "0.35" } });
  await fireEvent.click(screen.getByRole("button", { name: "Mute" }));
  expect(container.querySelector("audio")).toBeNull();
  await fireEvent.click(screen.getByRole("button", { name: "Play" }));
  await screen.findByRole("button", { name: "Pause" });
  expect(container.querySelector("audio")?.volume).toBeCloseTo(0.35);
  expect(container.querySelector("audio")?.muted).toBe(true);
  await fireEvent.click(screen.getByRole("button", { name: "Unmute" }));
  expect(container.querySelector("audio")?.muted).toBe(false);
});

it("keeps stepping available when the desktop media backend is missing", async () => {
  window.pywebview = { api: { choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn(), media_available: vi.fn(async () => false) } };
  const { container } = render(Transport, props);
  await fireEvent.click(screen.getByRole("button", { name: "Play" }));
  expect(await screen.findByText(/^Audio is unavailable/)).toHaveTextContent("Audio is unavailable");
  expect(container.querySelector("audio")).toBeNull();
  await fireEvent.click(screen.getByRole("button", { name: "Next frame" }));
  expect(props.onseek).toHaveBeenCalledWith(1);
});

it("does not resume playback after the page has closed", async () => {
  let finish: () => void = () => {};
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  props.onplaying.mockClear();
  const view = render(Transport, props);
  await fireEvent.click(screen.getByRole("button", { name: "Play" }));
  await waitFor(() => expect(HTMLMediaElement.prototype.play).toHaveBeenCalled());
  view.unmount();
  props.onplaying.mockClear();
  finish();
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(props.onplaying).not.toHaveBeenCalledWith(true);
});

it("shows a note if the desktop media check never replies", async () => {
  window.pywebview = { api: { choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn(), media_available: vi.fn(() => new Promise<boolean>(() => {})) } };
  vi.useFakeTimers();
  try {
    const { container } = render(Transport, { ...props, count: 1000, end: 1000 });
    await fireEvent.click(screen.getByRole("button", { name: "Play" }));
    await vi.advanceTimersByTimeAsync(5001);
    expect(screen.getByText(/^Audio could not play/)).toBeInTheDocument();
    expect(container.querySelector("audio")).toBeNull();
  } finally { vi.useRealTimers(); }
});

it("prepares frames while audio is opening and lets the user cancel startup", async () => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  const prepare = vi.fn();
  let finishCheck: (available: boolean) => void = () => {};
  window.pywebview = { api: { choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn(), media_available: vi.fn(() => new Promise<boolean>(resolve => { finishCheck = resolve; })) } };
  const view = render(Transport, { ...props, onprepare: prepare });
  try {
    await fireEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(prepare).toHaveBeenLastCalledWith(0);
    await fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(prepare).toHaveBeenLastCalledWith(null);
    finishCheck(false);
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.queryByText(/^Audio is unavailable/)).toBeNull();
    expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
  } finally { view.unmount(); }
});

it.each(["failure", "success"])("ignores an old audio-start %s after a cancelled start has been replaced", async (outcome) => {
  const attempts: { resolve: () => void; reject: (error: Error) => void }[] = [];
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => new Promise<void>((resolve, reject) => { attempts.push({ resolve, reject }); }));
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  const playing = vi.fn();
  const view = render(Transport, { ...props, onplaying: playing });
  try {
    await fireEvent.click(screen.getByRole("button", { name: "Play" }));
    await waitFor(() => expect(attempts).toHaveLength(1));
    await fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await fireEvent.click(screen.getByRole("button", { name: "Play" }));
    await waitFor(() => expect(attempts).toHaveLength(2));
    attempts[1]!.resolve();
    await screen.findByRole("button", { name: "Pause" });
    const pauses = vi.mocked(HTMLMediaElement.prototype.pause).mock.calls.length;
    if (outcome === "failure") attempts[0]!.reject(new Error("old start was cancelled"));
    else attempts[0]!.resolve();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.queryByText(/^Audio could not play/)).toBeNull();
    expect(playing.mock.calls.filter(([value]) => value === true)).toHaveLength(2);
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(pauses);
  } finally { view.unmount(); }
});

it("uses Space to play and pause and ignores a held Space key", async () => {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  render(Transport, props);
  await fireEvent.keyDown(window, { key: " ", code: "Space" });
  await screen.findByRole("button", { name: "Pause" });
  await fireEvent.keyDown(window, { key: " ", code: "Space", repeat: true });
  expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  await fireEvent.keyDown(window, { key: " ", code: "Space" });
  expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
});

it("steps one frame while paused and clamps at clip boundaries", async () => {
  const seek = vi.fn();
  const view = render(Transport, { ...props, index: 4, onseek: seek });
  await fireEvent.keyDown(window, { key: "ArrowLeft" });
  expect(seek).toHaveBeenLastCalledWith(3);
  await fireEvent.keyDown(window, { key: "ArrowRight" });
  expect(seek).toHaveBeenLastCalledWith(5);
  await view.rerender({ ...props, index: 9, onseek: seek });
  await fireEvent.keyDown(window, { key: "ArrowRight" });
  expect(seek).toHaveBeenLastCalledWith(9);
});

it("seeks five seconds from the audio clock during playback without pausing", async () => {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  const seek = vi.fn();
  const { container } = render(Transport, { ...props, count: 1000, end: 1000, onseek: seek });
  await fireEvent.keyDown(window, { key: " ", code: "Space" });
  await screen.findByRole("button", { name: "Pause" });
  await waitFor(() => expect(HTMLMediaElement.prototype.play).toHaveBeenCalled());
  const audio = container.querySelector("audio")!;
  audio.currentTime = 10;
  const pauses = pause.mock.calls.length;
  await fireEvent.keyDown(window, { key: "ArrowRight" });
  expect(audio.currentTime).toBe(15);
  expect(seek).toHaveBeenLastCalledWith(360);
  await fireEvent.keyDown(window, { key: "ArrowLeft" });
  expect(audio.currentTime).toBe(10);
  expect(seek).toHaveBeenLastCalledWith(240);
  audio.currentTime = 1;
  await fireEvent.keyDown(window, { key: "ArrowLeft" });
  expect(audio.currentTime).toBe(0);
  expect(pause).toHaveBeenCalledTimes(pauses);
  expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
});

it("leaves text fields outside the video and modified shortcuts alone", async () => {
  const seek = vi.fn();
  render(Transport, { ...props, onseek: seek });
  const input = document.createElement("input");
  document.body.append(input);
  try {
    await fireEvent.keyDown(input, { key: "ArrowRight" });
    await fireEvent.keyDown(input, { key: " ", code: "Space" });
    await fireEvent.keyDown(window, { key: "ArrowRight", ctrlKey: true });
    expect(seek).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
  } finally { input.remove(); }
});

it("moves the In and Out handles by drag and by keyboard, keeping the range at least a frame", async () => {
  const ontrim = vi.fn(), onscrub = vi.fn();
  const { container } = render(Transport, { ...props, start: 2, end: 8, ontrim, onscrub });
  const bar = container.querySelector(".bar") as HTMLElement;
  vi.spyOn(bar, "getBoundingClientRect").mockReturnValue({ left: 0, width: 100, top: 0, height: 10, right: 100, bottom: 10, x: 0, y: 0, toJSON: () => ({}) });
  const handle = screen.getByRole("slider", { name: "In mark" });
  expect(handle).toHaveAttribute("aria-valuetext", "In frame 3, 00:00.08");
  expect(screen.getByText("In 3 · 00:00.08")).toBeInTheDocument();
  expect(screen.getByText("Out 8 · 00:00.33")).toBeInTheDocument();
  await fireEvent.pointerDown(handle, { button: 0, clientX: 40, pointerId: 1 });
  await fireEvent.pointerMove(handle, { clientX: 95, pointerId: 1 });
  expect(onscrub).toHaveBeenLastCalledWith(7);  // never past the Out mark
  expect(ontrim).not.toHaveBeenCalled();
  await fireEvent.pointerUp(handle, { clientX: 95, pointerId: 1 });
  expect(ontrim).toHaveBeenCalledWith(7, 8);
  const out = screen.getByRole("slider", { name: "Out mark" });
  await fireEvent.keyDown(out, { key: "ArrowLeft" });
  expect(ontrim).toHaveBeenLastCalledWith(2, 7);
  await fireEvent.keyDown(out, { key: "End" });
  expect(ontrim).toHaveBeenLastCalledWith(2, 10);
  expect(screen.queryByRole("button", { name: "Mark In" })).toBeNull();
});

it("marks In and Out at the playhead with I and O", async () => {
  const ontrim = vi.fn();
  render(Transport, { ...props, index: 4, start: 0, end: 10, ontrim });
  await fireEvent.keyDown(window, { key: "i" });
  expect(ontrim).toHaveBeenLastCalledWith(4, 10);
  await fireEvent.keyDown(window, { key: "o" });
  expect(ontrim).toHaveBeenLastCalledWith(0, 5);
});

it("skips to the In and Out marks first, then to the clip's ends", async () => {
  const seek = vi.fn();
  const { rerender } = render(Transport, { ...props, index: 5, start: 2, end: 8, onseek: seek });
  await fireEvent.click(screen.getByRole("button", { name: "Skip to beginning" }));
  expect(seek).toHaveBeenLastCalledWith(2);
  await rerender({ index: 2 });
  await fireEvent.click(screen.getByRole("button", { name: "Skip to beginning" }));
  expect(seek).toHaveBeenLastCalledWith(0);
  await rerender({ index: 5 });
  await fireEvent.click(screen.getByRole("button", { name: "Skip to end" }));
  expect(seek).toHaveBeenLastCalledWith(7);  // the Out mark's frame
  await rerender({ index: 7 });
  await fireEvent.click(screen.getByRole("button", { name: "Skip to end" }));
  expect(seek).toHaveBeenLastCalledWith(9);
});

it("offers Replay at the Out mark and replays from the In mark", async () => {
  vi.spyOn(performance, "now").mockReturnValue(1000);
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  const prepare = vi.fn();
  const { container } = render(Transport, { ...props, index: 7, start: 2, end: 8, onprepare: prepare });
  expect(screen.getByRole("button", { name: "Replay" })).toHaveAttribute("title", "Replay from the In mark (Space)");
  await fireEvent.click(screen.getByRole("button", { name: "Replay" }));
  await screen.findByRole("button", { name: "Pause" });
  expect(prepare).toHaveBeenCalledWith(2);
  await waitFor(() => expect(container.querySelector("audio")?.currentTime).toBeCloseTo(2 / 24));
});

it("plays, pauses, skips and steps from any focused video control", async () => {
  const seek = vi.fn();
  render(Transport, { ...props, index: 4, onseek: seek });
  for (const control of [screen.getByRole("slider", { name: "Volume" }), screen.getByRole("slider", { name: "Position" }), screen.getByRole("button", { name: "Next frame" })]) {
    control.focus();
    const event = new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true, cancelable: true });
    control.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  }
  expect(seek).toHaveBeenCalledTimes(3);
  expect(seek).toHaveBeenLastCalledWith(3);
  const button = screen.getByRole("button", { name: "Next frame" });
  await fireEvent.keyDown(button, { key: " ", code: "Space" });
  const release = new KeyboardEvent("keyup", { key: " ", code: "Space", bubbles: true, cancelable: true });
  button.dispatchEvent(release);
  expect(release.defaultPrevented).toBe(true);  // Space plays; it does not also press the focused button
  await waitFor(() => expect(screen.getByRole("button", { name: /Pause|Cancel/ })).toBeInTheDocument());
});

it("leaves arrows on a focused trim handle to move its mark", async () => {
  const seek = vi.fn(), trim = vi.fn();
  render(Transport, { ...props, index: 4, onseek: seek, ontrim: trim, start: 2, end: 8 });
  await fireEvent.keyDown(screen.getByRole("slider", { name: "Out mark" }), { key: "ArrowLeft" });
  expect(trim).toHaveBeenCalled();
  expect(seek).not.toHaveBeenCalledWith(3);  // no frame step from the playhead (moving a mark shows it)
});

it("advances video immediately while audio startup is still pending, then aligns late audio", async () => {
  let now = 1000;
  vi.spyOn(performance, "now").mockImplementation(() => now);
  let advance: FrameRequestCallback = () => {};
  vi.spyOn(window, "requestAnimationFrame").mockImplementation(callback => { advance = callback; return 1; });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});
  let started: () => void = () => {};
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => new Promise<void>(resolve => { started = resolve; }));
  const seek = vi.fn(), playing = vi.fn();
  const view = render(Transport, { ...props, index: 24, start: 24, count: 1000, end: 1000, onseek: seek, onplaying: playing });
  await fireEvent.click(screen.getByRole("button", { name: "Play" }));
  await waitFor(() => expect(HTMLMediaElement.prototype.play).toHaveBeenCalled());
  expect(playing).toHaveBeenCalledWith(true);
  now = 2000; advance(now);
  expect(seek).toHaveBeenLastCalledWith(48);
  started();
  await Promise.resolve(); await Promise.resolve();
  await waitFor(() => expect(view.container.querySelector("audio")!.currentTime).toBeCloseTo(2));
});


it("keeps video playing without audio and stops at the Out mark", async () => {
  let now = 1000;
  vi.spyOn(performance, "now").mockImplementation(() => now);
  let advance: FrameRequestCallback = () => {};
  vi.spyOn(window, "requestAnimationFrame").mockImplementation(callback => { advance = callback; return 1; });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});
  window.pywebview = { api: { choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn(), media_available: vi.fn(async () => false) } };
  const seek = vi.fn();
  render(Transport, { ...props, index: 24, start: 24, count: 100, end: 72, onseek: seek });
  await fireEvent.click(screen.getByRole("button", { name: "Play" }));
  await screen.findByText(/Video will continue without sound/);
  now = 2000; advance(now);
  expect(seek).toHaveBeenLastCalledWith(48);
  now = 4000; advance(now);
  expect(seek).toHaveBeenLastCalledWith(71);
  await screen.findByRole("button", { name: "Play" });
});
