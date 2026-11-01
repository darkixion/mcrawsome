import { fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { clipViews, receive } from "../src/lib/library";
import { colourChoices } from "./colour-fixture";
import { openSections } from "../src/lib/sections";
import { stabilisedView } from "../src/lib/warp";
import DevelopInspectorHarness from "./DevelopInspectorHarness.svelte";

beforeEach(() => openSections.set({ source: true, stabilise: true, resolve: true, review: true, "raw-media": true, "raw-advanced": true }));


const sends: Record<string, unknown>[] = [];
let connections = 0;
let openingBlocked = false;
let clipGyro = false;
let clipKind = "raw";
let clipBlur: number | null = null;
class Socket {
  binaryType = "";
  onmessage: ((event: { data: string | ArrayBuffer }) => void) | null = null;
  onclose: (() => void) | null = null;
  constructor() { connections += 1; socket = this; queueMicrotask(() => { if (!openingBlocked) this.onmessage?.({ data: JSON.stringify({ kind: "opened", bytes: 0, frame_count: 240, rate: 24, width: 160, height: 90 }) }); }); }
  send(text: string) { sends.push(JSON.parse(text)); }
  reply(seq: number, kind: string, data = new ArrayBuffer(24), extra: Record<string, unknown> = {}) {
    this.onmessage?.({ data: JSON.stringify({ seq, kind, bytes: data.byteLength, width: 2, height: 2, size: 2, ...extra }) });
    this.onmessage?.({ data });
  }
  close() {}
}
let socket: Socket;
let failure = "";
let version = "v1";
beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  clipViews.set({});
  stabilisedView.set(false);
  sends.length = 0; connections = 0; openingBlocked = false; failure = ""; version = "v1"; clipGyro = false; clipKind = "raw"; clipBlur = null;
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/colour")) return Response.json(colourChoices);
    if (init?.method === "PUT") {
      if (failure) return Response.json({ error: { code: failure, field: "develop.rotation", message: "Orientation is invalid" } }, { status: failure === "recipe-changed" ? 409 : 422 });
      return Response.json({ version: "v2", values: JSON.parse(init.body as string).values });
    }
    if (url.endsWith("/recipe")) return Response.json({ layer: { version, values: {}, error: null }, resolved: { develop: { exposure: 0, highlight_recovery: 1 }, motion: { enabled: false, crop: 1.1 }, trim: {} } });
    if (url.endsWith("/analysis")) return Response.json({ Exposure: { Exposure: "+1.0 stops (set)" }, warnings: [], suggestions: [], suggested_exposure: 0.3 });
    if (url.endsWith("/looks")) return Response.json({ looks: [{ name: "neutral", description: "Neutral" }, { name: "gritty", description: "Gritty" }] });
    if (url.endsWith("/waveform")) return Response.json({ peaks: [0.1, 0.5, 1] });
    if (url.endsWith("/motion")) return Response.json({ movement: "handheld", shares: { handheld: 1 }, blurred: [], notes: [],
      trim: { start_s: 0.5, end_s: 9, reasons: ["Start: a handling jolt (up to 64 degrees/s) until 0.5 s", "End: a handling jolt (up to 70 degrees/s) from 9.0 s, usually pressing stop"] } });
    return Response.json({ id: "a", name: "a.mcraw", path: "/clips/a.mcraw", duration_s: 10, fps: 24, gyro: clipGyro, kind: clipKind, black_levels: [64,64,64,64], white_level: 4095, blurred_s: clipBlur });
  }));
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
const puts = () => vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "PUT");
// The same in the Library's pane, where the settings' action row stands in for the heading.
async function readyInPane() {
  await waitFor(() => expect(sends.some((request) => request.op === "recipe")).toBe(true));
  socket.reply(Number(sends.find((request) => request.op === "recipe")?.seq), "draft");
  await screen.findByRole("region", { name: "Develop selected clips" });
}

it("enables image-assisted refinement by default and saves the opt-out", async () => {
  clipGyro = true;
  render(DevelopInspectorHarness, { id: "a" });
  const toggle = await screen.findByRole("switch", { name: "Image-assisted refinement" });
  expect(toggle).toBeChecked();
  await fireEvent.click(toggle);
  await waitFor(() => expect(puts().length).toBeGreaterThan(0));
  expect(JSON.parse(String(puts().at(-1)?.[1]?.body)).values).toMatchObject({ motion: { optical_correction: false } });
});

it("reveals Proxy and saves optional Horizon Lock and motion markers outside RAW calibration", async () => {
  const original = fetch;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => url === "/api/v1/clips/a"
    ? Promise.resolve(Response.json({ id: "a", kind: "raw", name: "a.mcraw", gyro: true, width: 256, height: 144, black_levels: [64,64,64,64], white_level: 4095 }))
    : original(url, init)));
  render(DevelopInspectorHarness, { id: "a" });
  const proxy = await screen.findByLabelText("Proxy");
  expect(proxy).toHaveValue("full");
  expect(proxy.closest("details")).toBeNull();
  expect(screen.getByRole("switch", { name: "Horizon Lock" })).not.toBeChecked();
  expect(screen.getByRole("switch", { name: "Review markers" })).not.toBeChecked();
  await fireEvent.click(screen.getByRole("switch", { name: "Horizon Lock" }));
  await fireEvent.click(screen.getByRole("switch", { name: "Review markers" }));
  await fireEvent.change(proxy, { target: { value: "quarter" } });
  expect(sends.at(-1)?.recipe).toMatchObject({ motion: { stabilise: "off" } });
  await waitFor(() => expect(puts().length).toBeGreaterThan(0));
  expect(JSON.parse(String(puts().at(-1)?.[1]?.body)).values).toMatchObject({ develop: { mount_proxy: "quarter" }, motion: { horizon_lock: true, markers: true } });
});

it("folds the settings into sections that say what they are set to, with Stabilise open", async () => {
  openSections.set({});
  const original = fetch;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => url === "/api/v1/clips/a"
    ? Promise.resolve(Response.json({ id: "a", kind: "raw", name: "a.mcraw", gyro: true, width: 256, height: 144, black_levels: [64,64,64,64], white_level: 4095 }))
    : original(url, init)));
  render(DevelopInspectorHarness, { id: "a" });
  const stabilise = await screen.findByRole("button", { name: /^Stabilise(?!d)/ });
  await waitFor(() => expect(stabilise).toHaveTextContent("Smooth · crop 1.10 · strength 1.00"));
  expect(stabilise).toHaveAttribute("aria-expanded", "true");
  const source = screen.getByRole("button", { name: /^Source/ });
  expect(source).toHaveAttribute("aria-expanded", "false");
  expect(source).toHaveTextContent("As recorded");
  expect(screen.queryByRole("combobox", { name: "Orientation" })).toBeNull();
  const resolve = await screen.findByRole("button", { name: /^Resolve/ });
  expect(resolve).toHaveTextContent("Proxy off");
  const review = screen.getByRole("button", { name: /^Review markers/ });
  expect(review).toHaveTextContent("Off");
  // A switch in a folded heading still works, and the summary follows it.
  await fireEvent.click(screen.getByRole("switch", { name: "Review markers" }));
  expect(review).toHaveTextContent("8 of 8 kinds");
  expect(review).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByRole("button", { name: /^RAW media/ })).toHaveTextContent("Inherited levels · recorded rate");
  expect(screen.getByRole("button", { name: /^Advanced RAW/ })).toHaveAttribute("aria-expanded", "false");
  await fireEvent.click(source);
  expect(screen.getByRole("combobox", { name: "Orientation" })).toHaveValue("0");
});

it.each(["davinci-intermediate", "logc3"])("keeps both input defaults on Auto for detected %s", async (detectedTransfer) => {
  const original = fetch;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => url === "/api/v1/clips/a"
    ? Promise.resolve(Response.json({ id: "a", kind: "log", name: "recording.mp4", metadata: { Video: { transfer_id: detectedTransfer } } }))
    : original(url, init)));
  render(DevelopInspectorHarness, { id: "a" });
  const transfer = await screen.findByLabelText("Input transfer function");
  await waitFor(() => expect(transfer).toBeEnabled());
  expect(transfer).toHaveValue("auto");
  expect(screen.getByLabelText("Input colour space")).toHaveValue("auto");
  if (detectedTransfer === "logc3") {
    expect(await screen.findByLabelText("ARRI exposure index")).toHaveValue("800");
    await fireEvent.focus(screen.getByRole("button", { name: "About ARRI exposure index" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("separate from the exposure adjustment");
  } else expect(screen.queryByLabelText("ARRI exposure index")).toBeNull();
  expect(puts()).toHaveLength(0);
});

it("prompts for an unidentified recording profile before a preview can open", async () => {
  openingBlocked = true;
  const original = fetch;
  const note = "Choose the input colour space and transfer function in Develop.";
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => url === "/api/v1/clips/a"
    ? Promise.resolve(Response.json({ id: "a", kind: "log", name: "renamed.mp4", metadata: { Video: { colour_note: note } } }))
    : original(url, init)));
  render(DevelopInspectorHarness, { id: "a" });
  expect(await screen.findByText(note)).toBeVisible();
  await waitFor(() => expect(screen.getByLabelText("Input colour space")).toBeEnabled());
  await fireEvent.change(screen.getByLabelText("Input colour space"), { target: { value: "davinci-wg" } });
  expect(screen.getByText(note)).toBeVisible();
  await fireEvent.change(screen.getByLabelText("Input transfer function"), { target: { value: "davinci-intermediate" } });
  expect(screen.queryByText(note)).toBeNull();
  await waitFor(() => expect(puts().length).toBeGreaterThan(0));
  expect(JSON.parse(String(puts().at(-1)?.[1]?.body)).values.develop).toMatchObject({ input_gamut: "davinci-wg", input_transfer: "davinci-intermediate" });
});

it("migrates a legacy input pair atomically and clears ARRI EI when leaving LogC3", async () => {
  clipKind = "log";
  const original = fetch;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => url.endsWith("/recipe") && init?.method !== "PUT"
    ? Promise.resolve(Response.json({ layer: { version, values: { develop: { input_encoding: "acescct" } } }, resolved: { develop: { input_encoding: "acescct", input_ei: 800 } } }))
    : original(url, init)));
  render(DevelopInspectorHarness, { id: "a" });
  const transfer = await screen.findByLabelText("Input transfer function");
  await waitFor(() => expect(transfer).toHaveValue("acescct"));
  expect(screen.getByLabelText("Input colour space")).toHaveValue("ap1");
  sends.length = 0;
  await fireEvent.change(transfer, { target: { value: "logc3" } });
  await fireEvent.change(screen.getByLabelText("ARRI exposure index"), { target: { value: "400" } });
  expect(sends.at(-1)?.recipe).toMatchObject({ develop: { input_encoding: "auto", input_gamut: "ap1", input_transfer: "logc3", input_ei: 400 } });
  await fireEvent.change(transfer, { target: { value: "acescct" } });
  expect(sends.at(-1)?.recipe).toMatchObject({ develop: { input_encoding: "auto", input_ei: 800 } });
  expect(screen.queryByLabelText("ARRI exposure index")).toBeNull();
  expect(sends.every(request => (request.recipe as { develop?: { input_encoding?: string } })?.develop?.input_encoding === "auto")).toBe(true);
  await waitFor(() => expect(puts().length).toBeGreaterThan(0));
  expect(JSON.parse(String(puts().at(-1)?.[1]?.body)).values.develop).toMatchObject({ input_encoding: "auto", input_transfer: "acescct", input_ei: 800 });
});

it("makes Camera Native interpretation explicit and can restore Auto colour", async () => {
  clipKind = "log";
  render(DevelopInspectorHarness, { id: "a" });
  const gamut = await screen.findByLabelText("Input colour space");
  await waitFor(() => expect(gamut).toBeEnabled());
  await fireEvent.change(gamut, { target: { value: "camera-native" } });
  const native = screen.getByLabelText("Camera Native interpretation");
  expect(native).toHaveValue("unset");
  await fireEvent.change(native, { target: { value: "ap1" } });
  expect(sends.at(-1)?.recipe).toMatchObject({ develop: { input_gamut: "camera-native", native_as: "ap1" } });
  await fireEvent.click(screen.getByRole("button", { name: "Use Auto colour" }));
  expect(sends.at(-1)?.recipe).toMatchObject({ develop: { input_encoding: "auto", input_gamut: "auto", input_transfer: "auto", input_ei: 800, native_as: "unset" } });
});

it("shows the Library thumbnail and opens the session while the recipe is still loading", async () => {
  const original = fetch;
  let finish!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => url.endsWith("/recipe")
    ? new Promise<Response>(resolve => { finish = resolve; }) : original(url, init)));
  render(DevelopInspectorHarness, { id: "a" });
  expect(screen.getByRole("img", { name: "Library thumbnail" })).toHaveAttribute("src", "/api/v1/clips/a/thumbnail");
  await waitFor(() => expect(connections).toBe(1));
  expect(sends).toEqual([]);
  finish(Response.json({ layer: { version: "v1", values: {} }, resolved: {} }));
  await readyInPane();
  await screen.findByRole("img", { name: "Clip preview" });
  expect(screen.queryByRole("img", { name: "Library thumbnail" })).toBeNull();
});

it("lets the user scrub from clip metadata while the preview worker is opening", async () => {
  openingBlocked = true;
  render(DevelopInspectorHarness, { id: "a" });
  const position = await screen.findByRole("slider", { name: "Position" });
  expect(position).toHaveAttribute("max", "239");
  await fireEvent.input(position, { target: { value: 96 } });
  expect(sends).toEqual([]);
  socket.onmessage?.({ data: JSON.stringify({ kind: "opened", bytes: 0, frame_count: 240, rate: 24 }) });
  await waitFor(() => expect(sends.at(-1)).toMatchObject({ op: "recipe", index: 96 }));
});
it.each(["recipe-changed", "recipe-invalid"])("handles %s beside the controls", async (code) => {
  failure = code;
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  vi.useFakeTimers();
  await fireEvent.change(screen.getByRole("combobox", { name: "Orientation" }), { target: { value: 90 } });
  version = "other";
  await vi.advanceTimersByTimeAsync(500);
  vi.useRealTimers();
  if (code === "recipe-changed") {
    expect(await screen.findByText("This clip's settings were changed elsewhere; reloaded")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Orientation" })).toHaveValue("0");
  } else {
    expect(await screen.findByText("Orientation is invalid")).toBeVisible();
  }
});
it("shows a missing clip after a library removal event", async () => {
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  receive({ clips: [], removed: ["a"], removed_paths: [], missing_folders: [] });
  expect(await screen.findByText("This clip is no longer available")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Back to Library" })).toHaveAttribute("href", "#/library");
});
it("saves In and Out in the CLI time syntax and marks the bar", async () => {
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  await fireEvent.input(screen.getByRole("slider", { name: "Position" }), { target: { value: 48 } });
  await fireEvent.keyDown(window, { key: "i" });
  await fireEvent.input(screen.getByRole("slider", { name: "Position" }), { target: { value: 119 } });
  vi.useFakeTimers();
  await fireEvent.keyDown(window, { key: "o" });
  await vi.advanceTimersByTimeAsync(500);
  expect(JSON.parse(puts()[0]![1]!.body as string).values.trim).toEqual({ start: "2s", duration: "3s" });
  expect(screen.getByLabelText("Trim range")).toHaveAttribute("style", expect.stringContaining("20%"));
  expect(screen.getByRole("slider", { name: "Out mark" })).toHaveAttribute("aria-valuenow", "120");
});


it("keeps a second edit while a save is in flight and uses the returned version", async () => {
  const normal = vi.mocked(fetch).getMockImplementation()!;
  let finish: (response: Response) => void = () => {};
  vi.mocked(fetch).mockImplementation((url, init) => {
    if (init?.method === "PUT" && puts().length === 1) return new Promise((resolve) => { finish = resolve; });
    return normal(url, init);
  });
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  vi.useFakeTimers();
  await fireEvent.change(screen.getByRole("combobox", { name: "Orientation" }), { target: { value: 90 } });
  await vi.advanceTimersByTimeAsync(500);
  await fireEvent.change(screen.getByRole("combobox", { name: "Orientation" }), { target: { value: 180 } });
  await vi.advanceTimersByTimeAsync(500);
  expect(puts()).toHaveLength(1);
  finish(Response.json({ version: "v2", values: { develop: { rotation: 90 } } }));
  await vi.advanceTimersByTimeAsync(0);
  expect(puts()).toHaveLength(2);
  expect(JSON.parse(puts()[1]![1]!.body as string)).toMatchObject({ version: "v2", values: { develop: { rotation: 180 } } });
});

it("saves pending changes when returning to the Library", async () => {
  const view = render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  await fireEvent.change(screen.getByRole("combobox", { name: "Orientation" }), { target: { value: 90 } });
  view.unmount();
  expect(puts()).toHaveLength(1);
});



it("groups stabilisation under a Stabilise switch that greys its settings when off", async () => {
  clipGyro = true;
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  const section = screen.getByRole("region", { name: "Stabilise" });
  const control = within(section).getByRole("switch", { name: "Stabilise" });
  expect(control).toBeChecked();
  expect(within(section).getByRole("slider", { name: "Crop" })).toBeEnabled();
  expect(within(section).getByRole("switch", { name: "Rolling shutter compensation" })).toBeChecked();
  await fireEvent.click(control);
  expect(sends.at(-1)).toMatchObject({ recipe: { motion: { stabilise: "off" } } });
  for (const name of ["Crop", "Strength", "Smoothing (seconds)"]) expect(within(section).getByRole("slider", { name })).toBeDisabled();
  expect(within(section).getByRole("switch", { name: "Rolling shutter compensation" })).toBeDisabled();
  await fireEvent.click(control);
  expect(control).toBeChecked();
  await fireEvent.click(within(section).getByRole("switch", { name: "Rolling shutter compensation" }));
  await waitFor(() => expect(puts().length).toBeGreaterThan(0));
  expect(JSON.parse(String(puts().at(-1)?.[1]?.body)).values.motion).toMatchObject({ stabilise: "auto", bands: 1 });
});

it("saves the stabilisation mode and greys what each mode ignores", async () => {
  clipGyro = true;
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  const section = screen.getByRole("region", { name: "Stabilise" });
  const mode = within(section).getByLabelText("Mode");
  expect(mode).toHaveValue("smooth");
  await fireEvent.change(mode, { target: { value: "rolling_shutter" } });
  expect(within(section).getByRole("slider", { name: "Strength" })).toBeDisabled();
  expect(within(section).getByRole("slider", { name: "Smoothing (seconds)" })).toBeDisabled();
  await waitFor(() => expect(puts().length).toBeGreaterThan(0));
  expect(JSON.parse(String(puts().at(-1)?.[1]?.body)).values.motion).toMatchObject({ mode: "rolling_shutter" });
  expect(within(section).queryByText("Turn on Rolling shutter compensation for this mode.")).toBeNull();
  await fireEvent.click(within(section).getByRole("switch", { name: "Rolling shutter compensation" }));
  expect(within(section).getByText("Turn on Rolling shutter compensation for this mode.")).toBeInTheDocument();
  await fireEvent.change(mode, { target: { value: "tripod" } });
  expect(within(section).getByRole("slider", { name: "Strength" })).toBeEnabled();
  expect(within(section).getByRole("slider", { name: "Smoothing (seconds)" })).toBeDisabled();
  await fireEvent.change(mode, { target: { value: "pan" } });
  expect(within(section).getByRole("slider", { name: "Smoothing (seconds)" })).toBeEnabled();
  await waitFor(() => expect(JSON.parse(String(puts().at(-1)?.[1]?.body)).values.motion).toMatchObject({ mode: "pan", bands: 1 }));
});

it("explains the Stabilise sliders, keeps a crop margin and outlines the kept frame", async () => {
  clipGyro = true;
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  const section = screen.getByRole("region", { name: "Stabilise" });
  for (const name of ["Crop", "Strength", "Smoothing (seconds)"]) expect(within(section).getByRole("button", { name: `About ${name}` })).toBeInTheDocument();
  expect(within(section).getByRole("slider", { name: "Crop" })).toHaveAttribute("min", "1.05");
  expect(screen.getByRole("img", { name: "Stabilise crop 1.10: the outlined frame remains" })).toBeInTheDocument();
  await fireEvent.click(within(section).getByRole("switch", { name: "Stabilise" }));
  expect(screen.queryByRole("img", { name: /^Stabilise crop/ })).toBeNull();
});

it("moves the outlined frame once the clip is planned as a send plans it", async () => {
  clipGyro = true;
  const original = fetch;
  const asked: Record<string, unknown>[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    if (url !== "/api/v1/clips/a/kept-frame") return original(url, init);
    asked.push(JSON.parse(String(init?.body)));
    return Promise.resolve(Response.json({ corners: [[0.125, 0.125, 0.875, 0.125, 0.875, 0.875, 0.125, 0.875]], reason: null }));
  }));
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  const name = "Stabilise crop 1.10: the outlined frame remains, moving as the stabilisation will";
  expect(await screen.findByRole("img", { name }, { timeout: 3000 })).toBeInTheDocument();
  await waitFor(() => expect(asked).toHaveLength(2));
  expect(asked.map(body => body.quick)).toEqual([true, false]);  // a quick first look, then the send's plan
  expect(asked[1]!.recipe).toMatchObject({ motion: { crop: 1.1 } });
  const toggle = screen.getByRole("button", { name: "Stabilised" });
  expect(toggle).toHaveAttribute("aria-pressed", "false");
  await fireEvent.click(toggle);  // shown stabilised, the kept frame fills the picture: nothing to outline
  expect(toggle).toHaveAttribute("aria-pressed", "true");
  expect(screen.queryByRole("img", { name })).toBeNull();
  await fireEvent.click(toggle);
  expect(screen.getByRole("img", { name })).toBeInTheDocument();
});

it("says below the picture why a clip cannot be stabilised, and Stabilised no longer offers it", async () => {
  clipGyro = true;
  stabilisedView.set(true);  // left on from another clip
  const reason = "Horizon Lock lost reliable gravity or gyro coverage in the selected recording";
  const original = fetch;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    if (url !== "/api/v1/clips/a/kept-frame") return original(url, init);
    return Promise.resolve(Response.json({ corners: [], reason }));
  }));
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  const toggle = screen.getByRole("button", { name: "Stabilised" });
  expect(toggle).toHaveAttribute("title", "Shown as recorded until the stabilisation is planned");  // planning
  expect(toggle).toHaveAttribute("aria-pressed", "true");
  expect(toggle).toBeEnabled();
  const told = await screen.findByText(`Not stabilised: ${reason}`, undefined, { timeout: 3000 });
  expect(told).toHaveAttribute("role", "status");
  expect(toggle.compareDocumentPosition(told) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();  // the picture stays put
  expect(screen.getByRole("slider", { name: "Position" }).compareDocumentPosition(told) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(toggle).toBeDisabled();
  expect(toggle).toHaveAttribute("aria-pressed", "false");
  expect(toggle).toHaveAttribute("title", `Cannot show the picture as Resolve will stabilise it: ${reason}`);
  expect(screen.getByRole("img", { name: "Stabilise crop 1.10: the outlined frame remains" })).toBeInTheDocument();
  await fireEvent.click(within(screen.getByRole("region", { name: "Stabilise" })).getByRole("switch", { name: "Stabilise" }));
  expect(screen.queryByText(`Not stabilised: ${reason}`)).toBeNull();
});

it("greys out Stabilise without a gyro log and says why", async () => {
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  const section = screen.getByRole("region", { name: "Stabilise" });
  expect(within(section).getByRole("switch", { name: "Stabilise" })).toBeDisabled();
  expect(within(section).getByRole("slider", { name: "Crop" })).toBeDisabled();
  expect(within(section).getByText("No gyro log for this clip")).toBeInTheDocument();
});

it("reloads resolved controls after Reset during a save", async () => {
  const normal = vi.mocked(fetch).getMockImplementation()!;
  let finish: (response: Response) => void = () => {};
  vi.mocked(fetch).mockImplementation((url, init) => {
    if (init?.method === "PUT" && puts().length === 1) return new Promise((resolve) => { finish = resolve; });
    return normal(url, init);
  });
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  vi.useFakeTimers();
  await fireEvent.change(screen.getByRole("combobox", { name: "Orientation" }), { target: { value: 90 } });
  await vi.advanceTimersByTimeAsync(500);
  await fireEvent.click(screen.getByRole("button", { name: "Reset" }));
  finish(Response.json({ version: "v2", values: { develop: { rotation: 90 } } }));
  await vi.advanceTimersByTimeAsync(500);
  expect(JSON.parse(puts()[1]![1]!.body as string).values).toEqual({});
  expect(screen.getByRole("combobox", { name: "Orientation" })).toHaveValue("0");
});

it("drops late playback frames and catches up to the audio clock", async () => {
  let next: FrameRequestCallback = () => {};
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  let images = 0;
  vi.spyOn(URL, "createObjectURL").mockImplementation(() => `blob:shown-${++images}`);
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { next = callback; return 1; });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  const image = screen.getByRole("img", { name: /^Clip preview$/ });
  const first = image.getAttribute("src");
  await fireEvent.click(screen.getByRole("button", { name: /^Play$/ }));
  await waitFor(() => expect(screen.getByRole("button", { name: /^Pause$/ })).toBeInTheDocument());
  await waitFor(() => expect(HTMLMediaElement.prototype.play).toHaveBeenCalled());
  const requested = sends.at(-1)!;
  const count = sends.length;
  const audio = document.querySelector("audio")!;
  audio.currentTime = 1; next(1000);
  audio.currentTime = 2; next(2000);
  expect(sends).toHaveLength(count);
  socket.reply(Number(requested.seq), "draft", new ArrayBuffer(24), { index: 0, playback: true, buffer_end: true });
  expect(image.getAttribute("src")).toBe(first);
  const second = image.getAttribute("src");
  expect(sends.at(-1)).toMatchObject({ index: 48, playback: true });
  socket.reply(Number(sends.at(-1)?.seq), "draft", new ArrayBuffer(24), { index: 48, playback: true, buffer_end: true });
  await waitFor(() => expect(image.getAttribute("src")).not.toBe(second));
});


it("requests playback frames before audio finishes opening", async () => {
  let started = false;
  let finish: () => void = () => {};
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => { started = true; return new Promise<void>(resolve => { finish = resolve; }); });
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  await fireEvent.click(screen.getByRole("button", { name: "Play" }));
  await waitFor(() => expect(started).toBe(true));
  expect(sends.at(-1)).toMatchObject({ index: 0, playback: true, ahead: 3, fast: true });
  expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  finish();
  await screen.findByRole("button", { name: "Pause" });
});

it("can request source resolution without saving a clip recipe", async () => {
  render(DevelopInspectorHarness, { id: "a" });
  await waitFor(() => expect(sends.some(request => request.op === "recipe")).toBe(true));
  socket.onmessage?.({ data: JSON.stringify({ kind: "opened", bytes: 0, frame_count: 240, rate: 24, supports_source: true }) });
  const control = await screen.findByRole("combobox", { name: "Preview resolution" });
  await fireEvent.change(control, { target: { value: "source" } });
  expect(sends.at(-1)).toMatchObject({ source: true, index: 0 });
  expect(puts()).toHaveLength(0);
});

it("reports playback preparation and destruction to the Library diagnostics", async () => {
 const onplayback=vi.fn();let finish:()=>void=()=>{};
 vi.spyOn(HTMLMediaElement.prototype,"play").mockImplementation(()=>new Promise<void>(resolve=>{finish=resolve;}));
 const view=render(DevelopInspectorHarness,{id:"a",onplayback});
 await fireEvent.click(await screen.findByRole("button",{name:"Play"}));
 await waitFor(()=>expect(onplayback).toHaveBeenCalledWith(true));
 view.unmount();
 expect(onplayback).toHaveBeenLastCalledWith(false);
 finish();
});

it("restores inherited RAW calibration without discarding unrelated clip settings", async () => {
  const normal = fetch;
  const inherited = { black_level: 42, white_level: 3000, lens_shading: 0.7, lens_shading_mode: "colour" };
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "PUT") return normal(url, init);
    if (url.endsWith("/recipe")) return Promise.resolve(Response.json({
      layer: { version: "raw", values: { look: "gritty", develop: { exposure: 2, black_level: [10,20,30,40], white_level: 2000 } } },
      resolved: { look: "gritty", develop: { exposure: 2, ...inherited, black_level: [10,20,30,40], white_level: 2000 } },
      inherited: { develop: inherited },
    }));
    if (url === "/api/v1/clips/a") return Promise.resolve(Response.json({ id: "a", name: "a.mcraw", kind: "raw", black_levels: [64,65,66,67], white_level: 4095 }));
    return normal(url, init);
  }));
  render(DevelopInspectorHarness, { id: "a" });
  await screen.findByRole("combobox", { name: "White / Black Levels" });
  expect(screen.getByRole("spinbutton", { name: "Black top left" })).toHaveValue(10);
  await fireEvent.click(screen.getByRole("button", { name: "Use inherited black levels" }));
  expect(screen.getByRole("spinbutton", { name: "Black top left" })).toHaveValue(42);
  await fireEvent.click(screen.getByRole("button", { name: "Use inherited white level" }));
  expect(screen.getByRole("spinbutton", { name: "White level" })).toHaveValue(3000);
  await waitFor(() => expect(puts()).toHaveLength(1));
  expect(JSON.parse(puts()[0]![1]!.body as string)).toEqual({ version: "raw", values: { look: "gritty", develop: { exposure: 2 } } });
  expect(sends.at(-1)).toMatchObject({ recipe: { develop: { exposure: 0, ...inherited } } });
});

it("offers RAW calibration only for RAW clips with recorded levels", async () => {
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  expect(screen.queryByText("RAW calibration")).toBeNull();
});

it("previews and saves a RAW orientation turn as clip settings", async () => {
  const normal = fetch;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => url === "/api/v1/clips/a"
    ? Promise.resolve(Response.json({ id: "a", name: "a.mcraw", kind: "raw", width: 3840, height: 2160, black_levels: [64,64,64,64], white_level: 4095 }))
    : normal(url, init)));
  render(DevelopInspectorHarness, { id: "a" });
  const control = await screen.findByRole("combobox", { name: "Orientation" });
  await fireEvent.change(control, { target: { value: "90" } });
  await waitFor(() => expect(screen.getByLabelText("Video viewport")).toHaveAttribute("style", expect.stringContaining("--aspect: 2160 / 3840")));
  await fireEvent.change(control, { target: { value: "180" } });
  await waitFor(() => expect(screen.getByLabelText("Video viewport")).toHaveAttribute("style", expect.stringContaining("--aspect: 3840 / 2160")));
  await waitFor(() => expect(sends.at(-1)).toMatchObject({ op: "recipe", recipe: { develop: { rotation: 180 } } }));
  await waitFor(() => expect(puts()).toHaveLength(1));
  expect(JSON.parse(puts()[0]![1]!.body as string).values.develop.rotation).toBe(180);
  await fireEvent.change(control, { target: { value: "0" } });
  await waitFor(() => expect(sends.at(-1)).toMatchObject({ recipe: { develop: { rotation: 0 } } }));
});

it("shows gravity orientation advice and clears it when the chosen turn matches", async () => {
  const normal = fetch;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => url === "/api/v1/clips/a"
    ? Promise.resolve(Response.json({ id: "a", name: "a.mcraw", kind: "raw", black_levels: [64,64,64,64], white_level: 4095,
      orientation_hint: { appears_wrong: true, suggested_rotation: 90, confidence: 1, sampled_seconds: 5, message: "Orientation appears wrong" } }))
    : normal(url, init)));
  render(DevelopInspectorHarness, { id: "a" });
  expect(await screen.findByText(/Orientation appears wrong/)).toBeVisible();
  const control = screen.getByRole("combobox", { name: "Orientation" });
  await fireEvent.change(control, { target: { value: "90" } });
  expect(screen.queryByText(/Orientation appears wrong/)).toBeNull();
  await fireEvent.change(control, { target: { value: "180" } });
  expect(await screen.findByText(/Orientation appears wrong/)).toBeVisible();
});

it("flushes an immediate edit before handing settings to a mount and reports save failures", async () => {
  let flush: (() => Promise<boolean>) | null = null;
  render(DevelopInspectorHarness, { id: "a", onflush: value => { flush = value; } });
  await readyInPane();
  await fireEvent.change(screen.getByRole("combobox", { name: "Orientation" }), { target: { value: 90 } });
  expect(puts()).toHaveLength(0);
  expect(await flush!()).toBe(true);
  expect(JSON.parse(puts()[0]![1]!.body as string).values.develop.rotation).toBe(90);
  failure = "recipe-invalid";
  await fireEvent.change(screen.getByRole("combobox", { name: "Orientation" }), { target: { value: 180 } });
  expect(await flush!()).toBe(false);
  expect(await screen.findByText("Orientation is invalid")).toBeVisible();
});
it("announces a draft only when full quality takes more than a moment, and says what it means", async () => {
  render(DevelopInspectorHarness, { id: "a" });
  await waitFor(() => expect(sends.some((request) => request.op === "recipe")).toBe(true));
  vi.useFakeTimers();
  socket.reply(Number(sends.find((request) => request.op === "recipe")?.seq), "draft");
  await vi.advanceTimersByTimeAsync(300);
  expect(screen.queryByText(/full quality in progress/)).toBeNull();
  await vi.advanceTimersByTimeAsync(200);
  const status = screen.getByText("Quick draft shown · full quality in progress");
  expect(status).toHaveAttribute("title", expect.stringContaining("replaces the draft when it is ready"));
  socket.reply(Number(sends.at(-1)?.seq), "refined");
  await vi.advanceTimersByTimeAsync(0);
  expect(screen.queryByText(/full quality in progress/)).toBeNull();
  expect(screen.queryByText("Refining...")).toBeNull();
});

it("shows and plays the saved settings and marks in the Library's pane, where they can be changed", async () => {
  const { default: Pane } = await import("./DevelopInspectorHarness.svelte");
  const saved = { look: "gritty", hdr: false, develop: { exposure: 0.85, highlight_recovery: 1 }, motion: { enabled: false, crop: 1.5 }, grade: {}, denoise: {}, trim: { start: "2s", duration: "3s" } };
  const plain = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (url, init) => String(url).endsWith("/recipe") && init?.method !== "PUT"
    ? Response.json({ layer: { version: "v1", values: { develop: { exposure: 0.85 }, trim: saved.trim }, error: null }, resolved: saved })
    : plain(url, init));
  render(Pane, { id: "a" });
  await waitFor(() => expect(screen.getByRole("slider", { name: "In mark" })).toHaveAttribute("aria-valuenow", "48"));
  expect(screen.getByRole("slider", { name: "Out mark" })).toHaveAttribute("aria-valuenow", "120");
  expect(screen.queryByRole("slider", { name: "Exposure" })).toBeNull();
  await waitFor(() => expect(sends.find(request => request.op === "recipe")).toMatchObject({ recipe: { develop: { exposure: 0 } } }));
});

it("saves a LOG fixed-frame-rate choice and sends it to preview", async () => {
  clipKind = "log";
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  const control = await screen.findByRole("combobox", { name: "Frame rate" });
  await fireEvent.change(control, { target: { value: "25" } });
  await waitFor(() => expect(sends.some(request => (request.recipe as { output?: { frame_rate?: number } })?.output?.frame_rate === 25)).toBe(true));
  const changed = [...sends].reverse().find(request => (request.recipe as { output?: { frame_rate?: number } })?.output?.frame_rate === 25)!;
  socket.reply(Number(changed.seq), "draft", new ArrayBuffer(24), { frame_count: 125, rate: 25, index: 50 });
  await waitFor(() => expect(screen.getByLabelText("Frame position")).toHaveTextContent("51 / 125"));
  await waitFor(() => expect(puts().some(([,init]) => JSON.parse(init!.body as string).values.output?.frame_rate === 25)).toBe(true));
});


it("removes creative controls and requests a single neutral preview without altering saved legacy values", async () => {
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  for (const name of ["Look", "Preview", "Noise reduction"]) expect(screen.queryByRole("combobox", { name })).toBeNull();
  for (const name of ["Exposure", "Warmth", "Saturation", "Capture sharpening", "Luminance noise reduction"]) expect(screen.queryByRole("slider", { name })).toBeNull();
  expect(screen.queryByRole("switch", { name: "Before and after" })).toBeNull();
  expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith("/looks") || String(url).endsWith("/analysis"))).toBe(false);
  expect(sends.at(-1)).not.toHaveProperty("split");
  expect(sends.at(-1)).toMatchObject({ recipe: { develop: { exposure: 0 }, motion: { stabilise: "off" } } });
});

it("restores per-clip Source resolution and overlays, and keeps other clips independent", async () => {
  clipViews.set({ a: { resolution: "source", overlay: "focus" }, b: { resolution: "adaptive", overlay: "off" } });
  const original = fetch;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => url.endsWith("/preview-overlays")
    ? Promise.resolve(Response.json({ frames: 240, charts: [], notes: [] })) : original(url, init)));
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  socket.onmessage?.({ data: JSON.stringify({ kind: "opened", bytes: 0, frame_count: 240, rate: 24, supports_source: true, width: 160, height: 90 }) });
  const resolution = await screen.findByRole("combobox", { name: "Preview resolution" });
  expect(resolution).toHaveValue("source");
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/preview-overlays"), expect.anything()));
  await fireEvent.change(resolution, { target: { value: "adaptive" } });
  const { get } = await import("svelte/store");
  expect(get(clipViews)).toEqual({ a: { resolution: "adaptive", overlay: "focus" }, b: { resolution: "adaptive", overlay: "off" } });
});

it("defaults every review category on, gives toggles category colours and saves exclusions", async () => {
  render(DevelopInspectorHarness, { id: "a" });
  const master = await screen.findByRole("switch", { name: "Review markers" });
  await fireEvent.click(master);
  const categories = screen.getByRole("group", { name: "Review marker categories" });
  const toggles = within(categories).getAllByRole("switch");
  expect(toggles).toHaveLength(8);
  for (const toggle of toggles) expect(toggle).toBeChecked();
  expect(within(categories).getByRole("switch", { name: "Remaining tilt" })).toBeDisabled();
  const colours = toggles.map(toggle => toggle.closest("label")!.style.getPropertyValue("--accent"));
  expect(new Set(colours).size).toBe(8);
  await fireEvent.click(within(categories).getByRole("switch", { name: "Possible shake" }));
  await waitFor(() => expect(puts().some(([, init]) => JSON.parse(String(init?.body)).values.motion?.review_categories?.shake === false)).toBe(true));
  expect(within(categories).getByRole("switch", { name: "Recorded tilt" })).toBeChecked();
});


it("enables Remaining tilt only with stabilisation and preserves its category preference", async () => {
  clipGyro = true;
  render(DevelopInspectorHarness, { id: "a" });
  const master = await screen.findByRole("switch", { name: "Review markers" });
  await fireEvent.click(master);
  const remaining = screen.getByRole("switch", { name: "Remaining tilt" });
  const recorded = screen.getByRole("switch", { name: "Recorded tilt" });
  const stabilise = screen.getByRole("switch", { name: "Stabilise" });
  expect(remaining).toBeEnabled();
  await fireEvent.click(remaining);
  expect(remaining).not.toBeChecked();
  await fireEvent.click(stabilise);
  expect(remaining).toBeDisabled();
  expect(recorded).toBeEnabled();
  await fireEvent.click(stabilise);
  expect(remaining).toBeEnabled();
  expect(remaining).not.toBeChecked();
});

it("enables image-assisted refinement by default and saves the opt-out", async () => {
  clipGyro = true;
  render(DevelopInspectorHarness, { id: "a" });
  const toggle = await screen.findByRole("switch", { name: "Image-assisted refinement" });
  expect(toggle).toBeChecked();
  await fireEvent.click(toggle);
  await waitFor(() => expect(puts().length).toBeGreaterThan(0));
  expect(JSON.parse(String(puts().at(-1)?.[1]?.body)).values).toMatchObject({ motion: { optical_correction: false } });
});

it("suggests trim marks from the gyro log in the settings' action row, saves them and says why", async () => {
  clipGyro = true;
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  const actions = await screen.findByRole("region", { name: "Develop selected clips" });
  // Not above the video: the action row offers it, once, as it does Reset.
  expect(screen.getAllByRole("button", { name: "Suggest trim" })).toHaveLength(1);
  expect(screen.getAllByRole("button", { name: "Reset" })).toHaveLength(1);
  await fireEvent.click(within(actions).getByRole("button", { name: "Suggest trim" }));
  await waitFor(() => expect(puts().some(([, init]) => JSON.parse(String(init?.body)).values.trim?.start === "0.5s")).toBe(true));
  expect(JSON.parse(String(puts().at(-1)?.[1]?.body)).values.trim).toEqual({ start: "0.5s", duration: "8.5s" });
  expect(await within(actions).findByText(/usually pressing stop/)).toBeInTheDocument();
});

it("offers no trim suggestion without a gyro log", async () => {
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  expect(screen.queryByRole("button", { name: "Suggest trim" })).toBeNull();
});

it("warns under Stabilise when shake blur will remain after steadying", async () => {
  clipGyro = true; clipBlur = 2.5;
  render(DevelopInspectorHarness, { id: "a" });
  expect(await screen.findByText(/2.5 s of motion blur from shake/)).toBeInTheDocument();
  const master = await screen.findByRole("switch", { name: "Review markers" });
  await fireEvent.click(master);
  expect(within(screen.getByRole("group", { name: "Review marker categories" })).getByRole("switch", { name: "Motion blur" })).toBeChecked();
});

it("draws the clip's sound on the trim bar", async () => {
  render(DevelopInspectorHarness, { id: "a" });
  await readyInPane();
  await waitFor(() => expect(document.querySelector(".bar svg.wave path")).not.toBeNull());
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url) === "/api/v1/clips/a/waveform")).toHaveLength(1);
});
