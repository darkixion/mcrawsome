import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { get } from "svelte/store";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import Library from "../src/screens/Library.svelte";
import { connect, mediaUse } from "../src/lib/events";
import { clips, openedClips, mediaView, previewDocked } from "../src/lib/library";
import { clearAnnotationSessions } from "../src/lib/annotations";
import { selection } from "../src/lib/selection";
import { sendActivity, sendHistory, preferredTarget } from "../src/lib/send";
import { settings } from "../src/lib/settings";
import { FakeEventSource } from "./fake-event-source";
import { warmPreview, hoverPreview } from "../src/lib/preview-opening";

vi.mock("../src/screens/Develop.svelte", async () => ({ default: (await import("./LibraryPreviewStub.svelte")).default }));

vi.mock("../src/lib/preview-opening", () => ({ warmPreview: vi.fn(), hoverPreview: vi.fn(), leavePreview: vi.fn(), discardWarmPreview: vi.fn() }));

type Clip = Record<string, unknown>;
const clip = (id: string, extra: Clip = {}): Clip => ({
  id, path: `/clips/${id}.mcraw`, paths: [`/clips/${id}.mcraw`], name: `${id}.mcraw`, kind: "raw",
  duration_s: 18.2, width: 3840, height: 2160, fps: 24, captured: `2026-10-0${id.length}T10:00:00Z`,
  camera: "Pixel 10 Pro XL", status: "ready", badges: [], error: null, ...extra,
});
const details = (summary: Clip, extra: Clip = {}): Clip => ({
  ...summary, gyro: false, dropped_frames: 0, filled_frames: 0, recovered_note: null, camera_profile: null,
  recipe: { path: "/clips/a.mcraw.toml", values: {}, version: "1" }, ...extra,
});
const saved = { folders: ["/clips", "/gone"], destination: null, workers: null, keep_engine: false, resolve_route: "auto" };
let listed: Clip[];
let detailed: Record<string, Clip>;
let stop = () => {};

beforeEach(() => {
  window.location.hash = "#/library";
  clearAnnotationSessions();
  openedClips.set([]); mediaView.set(null); previewDocked.set(false);
  mediaUse.set({});
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.mocked(warmPreview).mockClear();
  clips.set(null);
  selection.set([]);
  sendActivity.set(null); sendHistory.set([]); preferredTarget.set("resolve"); localStorage.clear();
  settings.set(saved as never);
  FakeEventSource.instances = [];
  listed = [
    clip("a"),
    clip("bb", { kind: "log", name: "bb.mp4", path: "/clips/bb.mp4", badges: ["has-settings"] }),
    clip("ccc", { badges: ["recovered"] }),
    { ...clip("x"), id: null, name: "copy.mcraw", path: "/clips/copy.mcraw", status: "copying", duration_s: null, width: null, height: null, fps: null },
  ];
  detailed = {};
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (String(url).endsWith("/annotations")) return Response.json({ values: {}, version: "", storage: "sidecar", message: "Saved beside the recording", document: {} });
    if (url === "/api/v1/opened-clips") return Response.json({ clips: [], missing_folders: [] });
    if (url === "/api/v1/library") return Response.json({ clips: listed, missing_folders: ["/gone"] });
    if (url === "/api/v1/session") return Response.json({ name: "Untitled", path: null, portable: false, workspace: { paths: [], selection: [], focused: null, kind: "all", sort: "captured", search: "", facets: {} }, clips: [], missing: [], recent: [], problem: null });
    if (url === "/api/v1/mounts") return Response.json({ available: true, reason: "Available", mounts: [] });
    if (url === "/api/v1/jobs") return Response.json({ jobs: [] });
    const id = /^\/api\/v1\/clips\/([^/]+)$/.exec(url)?.[1];
    if (id !== undefined) return Response.json(detailed[id] ?? details(listed.find((item) => item.id === id)!));
    return Response.json({ error: { code: "input-missing", message: "no such route" } }, { status: 404 });
  }));
  stop = connect((url) => new FakeEventSource(url) as unknown as EventSource);
});
afterEach(() => { cleanup(); stop(); vi.unstubAllGlobals(); });

const card = (name: string) => screen.findByRole("option", { name });
const cards = () => within(screen.getByRole("listbox", { name: "Clips" })).getAllByRole("option").map((option) => option.getAttribute("aria-label"));

it("offers Media Bin and Timeline sends, whether or not the clips have gyro logs", async () => {
  listed[0] = clip("a", { gyro: true });
  listed[2] = clip("ccc", { gyro: false });
  render(Library);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("checkbox", { name: "Select a.mcraw" }));
  await user.click(screen.getByRole("checkbox", { name: "Select ccc.mcraw" }));
  await user.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
  const menu = await screen.findByRole("menu", { name: "Clip actions" });
  expect(within(menu).getAllByRole("menuitem", { name: /^Send to/ }).map(item => item.textContent)).toEqual([
    "Send to Resolve (Media Bin)", "Send to Resolve (Timeline)",
  ]);
  expect(within(menu).queryByRole("menuitem", { name: /Gyro/ })).toBeNull();
});

it("shares selection actions and opens every distinct source folder", async () => {
  listed[2] = clip("ccc", { path: "/other/ccc.mcraw", paths: ["/other/ccc.mcraw", "/split/ccc.1.mcraw"] });
  const open_folder = vi.fn(async (_path: string) => {});
  window.pywebview = { api: { open_folder, choose_folder: vi.fn(), restart_engine: vi.fn() } };
  try {
    render(Library);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("checkbox", { name: "Select a.mcraw" }));
    await user.click(screen.getByRole("checkbox", { name: "Select ccc.mcraw" }));
    await user.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
    const menu = await screen.findByRole("menu", { name: "Clip actions" });
    for (const label of ["Send to Resolve (Media Bin)", "Send to Resolve (Timeline)", "Export Vignette Profile...", "Apply Vignette Profile to Selected...", "Clear Vignette Profile Override", "Open Folder"]) expect(within(menu).getByRole("menuitem", { name: label })).toBeInTheDocument();
    expect(menu).not.toHaveTextContent("Soft Delete");
    expect(menu).toHaveTextContent("2 selected");
    await user.click(within(menu).getByRole("menuitem", { name: "Open Folder" }));
    expect(open_folder.mock.calls.map(call => call[0])).toEqual(["/clips", "/other", "/split"]);
    const row = await card("a.mcraw");
    row.focus();
    row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    await screen.findByRole("menu");
    expect(get(selection)).toEqual(["a", "ccc"]);
    await user.keyboard("{End}");
    expect(screen.getByRole("menuitem", { name: "Unmount and Move to Trash..." })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(row).toHaveFocus();
  } finally { delete window.pywebview; }
});

it("applies profiles to selected RAW clips and reports individual failures", async () => {
  const profile = { version: 1, sensor_width: 128, sensor_height: 64, cfa: "rggb", map_width: 2, map_height: 2, gains: Array.from({ length: 4 }, () => [2,2,2,2]) };
  window.pywebview = { api: { open_folder: vi.fn(), choose_folder: vi.fn(), restart_engine: vi.fn(), load_vignette_profile: vi.fn(async () => profile) } };
  const base = vi.mocked(fetch).getMockImplementation()!;
  const updates: string[] = [];
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/recipe")) return Response.json({ layer: { values: {}, version: "latest" }, resolved: {} });
    if (url.endsWith("/vignette-profile")) {
      updates.push(url);
      expect(JSON.parse(String(init?.body))).toEqual({ version: "latest", profile });
      return url.includes("/ccc/") ? Response.json({ error: { code: "recipe-invalid", message: "Sensor dimensions differ" } }, { status: 422 }) : Response.json({ values: {}, version: "next" });
    }
    return base(input, init);
  });
  try {
    render(Library);
    const user = userEvent.setup();
    for (const name of ["a.mcraw", "bb.mp4", "ccc.mcraw"]) await user.click(await screen.findByRole("checkbox", { name: `Select ${name}` }));
    await user.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
    await user.click(screen.getByRole("menuitem", { name: "Apply Vignette Profile to Selected..." }));
    const panel = await screen.findByRole("region", { name: "Vignette profile" });
    await waitFor(() => expect(updates).toHaveLength(2));
    expect(panel).toHaveTextContent("a.mcraw: Applied");
    expect(panel).toHaveTextContent("ccc.mcraw: Sensor dimensions differ");
    expect(updates).not.toContain("/api/v1/clips/bb/vignette-profile");
  } finally { delete window.pywebview; }
});

it("shows the engine's values formatted on each card", async () => {
  render(Library);
  const a = await card("a.mcraw");
  expect(within(a).getByText("00:18")).toBeInTheDocument();
  expect(within(a).getByText("4K")).toBeInTheDocument();
  expect(within(a).getByText("RAW")).toBeInTheDocument();
  expect(a.querySelector("img")).toHaveAttribute("src", "/api/v1/clips/a/thumbnail");
  expect(a.querySelector("img")).toHaveAttribute("loading", "lazy");
  const bb = await card("bb.mp4");
  expect(within(bb).getByText("LOG")).toBeInTheDocument();
  expect(within(bb).getByText("Clip settings")).toBeInTheDocument();
  expect(within(bb).queryByText("Has settings")).toBeNull();
  expect(within(await card("ccc.mcraw")).getByText("Recovered")).toBeInTheDocument();
});

it("preopens a ready preview on hover, keyboard focus and selection", async () => {
  render(Library);
  const a = await card("a.mcraw");
  a.dispatchEvent(new Event("pointerenter"));
  expect(hoverPreview).toHaveBeenCalledWith("a");
  vi.mocked(warmPreview).mockClear();
  a.focus();
  expect(warmPreview).toHaveBeenCalledWith("a");
  await userEvent.setup().click(await card("bb.mp4"));
  expect(warmPreview).toHaveBeenCalledWith("bb");
});

it("does not open a clip that is still copying", async () => {
  listed = listed.filter(item => item.status === "copying");
  render(Library);
  const copying = await card("copy.mcraw");
  expect(copying).toHaveAttribute("aria-disabled", "true");
  expect(within(copying).getByText("Copying...")).toBeInTheDocument();
  await userEvent.setup().click(copying);
  expect(get(selection)).toEqual([]);
  expect(screen.queryByRole("complementary", { name: "Clip details" })).toBeNull();
  expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).startsWith("/api/v1/clips/"))).toBe(false);
});

it("shows an unreadable clip with the engine's message", async () => {
  listed.push({ ...clip("y"), id: null, name: "bad.mcraw", path: "/clips/bad.mcraw", status: "unreadable", error: "the frame index is damaged" });
  render(Library);
  expect(within(await card("bad.mcraw")).getByText("the frame index is damaged")).toBeInTheDocument();
});

it("hides LOG clips under the RAW filter and finds clips by name", async () => {
  render(Library);
  await card("bb.mp4");
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "RAW" }));
  expect(screen.queryByRole("option", { name: "bb.mp4" })).toBeNull();
  expect(screen.getByRole("option", { name: "a.mcraw" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "All" }));
  await user.type(screen.getByRole("searchbox", { name: "Search names and details" }), "CC");
  expect(cards()).toEqual(["ccc.mcraw"]);
});

it("finds clips by their own details as well as their names", async () => {
  listed[0] = clip("a", { details_text: "Harbour wall\nSunrise\n12B" });
  render(Library);
  await card("a.mcraw");
  const user = userEvent.setup();
  const search = screen.getByRole("searchbox", { name: "Search names and details" });
  await user.type(search, "harbour");
  expect(cards()).toEqual(["a.mcraw"]);
  await user.clear(search);
  await user.type(search, "12b");
  expect(cards()).toEqual(["a.mcraw"]);
  await user.clear(search);
  await user.type(search, "nowhere");
  expect(screen.queryByRole("option", { name: "a.mcraw" })).toBeNull();
});

it("keeps capture order without a sorting control", async () => {
  render(Library);
  await card("a.mcraw");
  expect(cards()).toEqual(["ccc.mcraw", "bb.mp4", "a.mcraw", "copy.mcraw"]);
  expect(screen.queryByRole("combobox", { name: "Sort" })).toBeNull();
});

it("selects with click, Ctrl click and a Shift click range", async () => {
  render(Library);
  const user = userEvent.setup();
  await user.click(await card("ccc.mcraw"));
  expect(get(selection)).toEqual(["ccc"]);
  await user.keyboard("{Shift>}");
  await user.click(await card("a.mcraw"));
  await user.keyboard("{/Shift}");
  expect(get(selection)).toEqual(["ccc", "bb", "a"]);
  await user.keyboard("{Control>}");
  await user.click(await card("bb.mp4"));
  await user.keyboard("{/Control}");
  expect(get(selection)).toEqual(["ccc", "a"]);
  await user.click(await card("a.mcraw"));
  expect(get(selection)).toEqual(["a"]);
  expect(await card("a.mcraw")).toHaveAttribute("aria-selected", "true");
});

it("selects multiple clips with checkboxes and offers a shared bulk toolbar", async () => {
  render(Library);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("checkbox", { name: "Select a.mcraw" }));
  await user.click(screen.getByRole("checkbox", { name: "Select bb.mp4" }));
  expect(get(selection)).toEqual(["a", "bb"]);
  const bulk = screen.getByRole("toolbar", { name: "Selected clips" });
  expect(within(bulk).getByText("2 selected")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
  expect(screen.queryByRole("menuitem", { name: "Export..." })).toBeNull();
  expect(screen.getByRole("menuitem", { name: "Send to Resolve (Media Bin)" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "Develop in Library" })).toBeNull();
  await user.keyboard("{Escape}");
  expect(within(bulk).queryByRole("button", { name: "Actions" })).toBeNull();
  expect(get(selection)).toEqual(["a", "bb"]);
  await user.click(within(bulk).getByRole("checkbox", { name: "Select clips in this view" }));
  expect(new Set(get(selection))).toEqual(new Set(["a", "bb", "ccc"]));
  expect(screen.getByRole("checkbox", { name: "Select copy.mcraw" })).toBeDisabled();
  await user.click(within(bulk).getByRole("checkbox", { name: "Select clips in this view" }));
  expect(get(selection)).toEqual([]);
});

it("adds a card when a library event adds a clip, without a reload", async () => {
  render(Library);
  await card("a.mcraw");
  const loads = vi.mocked(fetch).mock.calls.filter(([url]) => url === "/api/v1/library").length;
  FakeEventSource.instances[0]?.send("library", { clips: [clip("dddd", { name: "new.mcraw" })], removed: ["a"], removed_paths: ["/clips/copy.mcraw"], missing_folders: [] });
  expect(await card("new.mcraw")).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByRole("option", { name: "a.mcraw" })).toBeNull());
  expect(screen.queryByRole("option", { name: "copy.mcraw" })).toBeNull();
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => url === "/api/v1/library")).toHaveLength(loads);
});

it("shows Gyro log found only when the engine found one", async () => {
  detailed.a = details(listed[0]!, { gyro: true, camera_profile: "Pixel 10 Pro XL main" });
  render(Library);
  const user = userEvent.setup();
  await user.click(await card("a.mcraw"));
  await userEvent.setup().click(screen.getByRole("tab", { name: "Clip info" }));
  let panel = await screen.findByRole("complementary", { name: "Clip details" });
  expect(await within(panel).findByText("Gyro log found")).toBeInTheDocument();
  expect(within(panel).getByText("Pixel 10 Pro XL main")).toBeInTheDocument();
  expect(within(panel).getByText("3840 x 2160 · 24 fps")).toBeInTheDocument();
  await user.click(await card("bb.mp4"));
  panel = await screen.findByRole("complementary", { name: "Clip details" });
  expect(await within(panel).findByText("No gyro log")).toBeInTheDocument();
  expect(within(panel).queryByText("Gyro log found")).toBeNull();
});

it("shows the fill count only when frames are filled", async () => {
  detailed.a = details(listed[0]!, { dropped_frames: 3, filled_frames: 3 });
  detailed.ccc = details(listed[2]!, { dropped_frames: 2, filled_frames: 0, recovered_note: "Recovered from an interrupted recording" });
  render(Library);
  const user = userEvent.setup();
  await user.click(await card("a.mcraw"));
  await userEvent.setup().click(screen.getByRole("tab", { name: "Clip info" }));
  let panel = await screen.findByRole("complementary", { name: "Clip details" });
  expect(await within(panel).findByText("3 dropped, 3 filled on export")).toBeInTheDocument();
  await user.click(await card("ccc.mcraw"));
  panel = await screen.findByRole("complementary", { name: "Clip details" });
  expect(await within(panel).findByText("2 dropped")).toBeInTheDocument();
  expect(within(panel).queryByText(/filled on export/)).toBeNull();
  expect(within(panel).getByText("Recovered from an interrupted recording")).toBeInTheDocument();
});


it("opens Develop for the clip shown", async () => {
  render(Library);
  await userEvent.setup().click(await card("a.mcraw"));
  await userEvent.setup().click(screen.getByRole("tab", { name: "Clip info" }));
  expect(await screen.findByRole("complementary", { name: "Clip details" })).toBeInTheDocument();
  await userEvent.setup().click(screen.getByRole("tab", { name: "Resolve Settings" }));
  expect(screen.getByText("Development controls a")).toBeVisible();
  expect(window.location.hash).toBe("#/library");
  window.location.hash = "";
});

it("shows the folders, with a missing one marked not found", async () => {
  render(Library);
  await card("a.mcraw");
  const bar = screen.getByRole("list", { name: "Folders" });
  expect(within(bar).getByText("clips")).toBeInTheDocument();
  expect(within(within(bar).getByTitle("/gone")).getByText("not found")).toBeInTheDocument();
  expect(within(within(bar).getByTitle("/clips")).queryByText("not found")).toBeNull();
});

it("asks for a folder with the window's dialog when there are no clips", async () => {
  listed = [];
  const choose_folder = vi.fn(async () => "/new");
  window.pywebview = { api: { open_folder: vi.fn(), choose_folder, restart_engine: vi.fn() } };
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (init?.method === "PUT") return Response.json({ ...saved, folders: ["/clips", "/gone", "/new"] });
    if (String(url).endsWith("/annotations")) return Response.json({ values: {}, version: "", storage: "sidecar", message: "Saved beside the recording", document: {} });
    if (url === "/api/v1/library") return Response.json({ clips: listed, missing_folders: [] });
    return Response.json({ jobs: [] });
  });
  try {
    render(Library);
    expect(await screen.findByText("Add a folder of MotionCam clips")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Choose a folder" }));
    expect(choose_folder).toHaveBeenCalled();
    const put = vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === "PUT");
    expect(JSON.parse(put?.[1]?.body as string).folders).toEqual(["/clips", "/gone", "/new"]);
  } finally { delete window.pywebview; }
});

it("takes a folder path when there is no window dialog", async () => {
  listed = [];
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (init?.method === "PUT") return Response.json({ ...saved, folders: ["/clips", "/gone", "/typed"] });
    if (String(url).endsWith("/annotations")) return Response.json({ values: {}, version: "", storage: "sidecar", message: "Saved beside the recording", document: {} });
    if (url === "/api/v1/library") return Response.json({ clips: listed, missing_folders: [] });
    return Response.json({ jobs: [] });
  });
  render(Library);
  const user = userEvent.setup();
  await user.type(await screen.findByRole("textbox", { name: "Folder path" }), "/typed");
  await user.click(screen.getByRole("button", { name: "Add folder" }));
  const put = vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === "PUT");
  expect(JSON.parse(put?.[1]?.body as string).folders).toEqual(["/clips", "/gone", "/typed"]);
});

it("keeps selected clips hidden by a filter on Ctrl click", async () => {
  render(Library);
  const user = userEvent.setup();
  await user.click(await card("bb.mp4"));
  await user.click(screen.getByRole("button", { name: "RAW" }));
  await user.keyboard("{Control>}");
  await user.click(await card("a.mcraw"));
  await user.keyboard("{/Control}");
  expect(get(selection)).toEqual(["bb", "a"]);
});

it("sees a folder dialog injected after rendering", async () => {
  listed = [];
  render(Library);
  await screen.findByRole("textbox", { name: "Folder path" });
  window.pywebview = { api: { open_folder: vi.fn(), choose_folder: vi.fn(async () => null), restart_engine: vi.fn() } };
  try {
    window.dispatchEvent(new Event("pywebviewready"));
    expect(await screen.findByRole("button", { name: "Choose a folder" })).toBeInTheDocument();
  } finally { delete window.pywebview; }
});

it("refreshes details when the shown clip changes in a library event", async () => {
  render(Library);
  await userEvent.setup().click(await card("a.mcraw"));
  await userEvent.setup().click(screen.getByRole("tab", { name: "Clip info" }));
  let panel = screen.getByRole("complementary", { name: "Clip details" });
  await within(panel).findByText("No gyro log");
  detailed.a = details(listed[0]!, { gyro: true });
  FakeEventSource.instances[0]?.send("library", { clips: [clip("a", { badges: ["has-settings"] })], removed: [], removed_paths: [], missing_folders: [] });
  expect(await within(panel).findByText("Gyro log found")).toBeInTheDocument();
});

it("clears the previous problem while reading another clip", async () => {
  render(Library);
  const user = userEvent.setup();
  await card("a.mcraw");
  const base = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((url, init) => String(url) === "/api/v1/clips/a"
    ? Promise.resolve(Response.json({ error: { code: "input-unreadable", message: "old problem" } }, { status: 422 })) : base(url, init));
  await user.click(await card("a.mcraw"));
  await userEvent.setup().click(screen.getByRole("tab", { name: "Clip info" }));
  let panel = screen.getByRole("complementary", { name: "Clip details" });
  await within(panel).findByText("old problem");
  vi.mocked(fetch).mockImplementation((url, init) => String(url) === "/api/v1/clips/bb" ? new Promise(() => {}) : base(url, init));
  await user.click(await card("bb.mp4"));
  panel = screen.getByRole("complementary", { name: "Clip details" });
  expect(within(panel).queryByText("old problem")).toBeNull();
  expect(within(panel).getByText("Reading the clip...")).toBeInTheDocument();
});

it("shows folder basenames for either separator", async () => {
  settings.set({ ...saved, folders: ["C:\\clips\\day\\", "/clips/night/"] } as never);
  render(Library);
  const bar = screen.getByRole("list", { name: "Folders" });
  expect(within(bar).getByText("day")).toBeInTheDocument();
  expect(within(bar).getByText("night")).toBeInTheDocument();
});

it("falls back to the file name when a thumbnail fails", async () => {
  render(Library);
  const option = await card("a.mcraw");
  const image = option.querySelector("img")!;
  image.dispatchEvent(new Event("error"));
  await waitFor(() => expect(option.querySelector("img")).toBeNull());
  expect(within(option).getAllByText("a.mcraw")).toHaveLength(2);
});

it("names the folder immediately while settings and the scan are pending", async () => {
  listed = [];
  render(Library);
  const user = userEvent.setup();
  await user.type(await screen.findByRole("textbox", { name: "Folder path" }), "/slow");
  vi.mocked(fetch).mockImplementation(() => new Promise(() => {}));
  await user.click(screen.getByRole("button", { name: "Add folder" }));
  expect(screen.getByRole("status", { name: "" })).toHaveTextContent("Reading the folder... /slow");
});

const rawPlan = {
  clips: [{ id: "a", output: "/clips/mcrawsome/a_DNG", notes: [], ignored: [], estimated_bytes: 2e9, checks: { audio_aligned: true, verified: true, dropped_filled: 0 }, estimated_seconds: 30, estimate_reason: null, exists: false }],
  estimated_bytes: 2e9, required_bytes: 2.2e9, free_bytes: 5e11, fits: true,
  checks: { audio_aligned: true, verified: true, dropped_filled: 0 }, space_message: null, estimated_seconds: 30, estimate_reason: null,
};
const posted: { url: string; body: unknown }[] = [];
function engineForSends(): void {
  posted.length = 0;
  const library = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST") posted.push({ url, body: JSON.parse(String(init?.body)) });
    if (url === "/api/v1/mounts") return Response.json({ available: true, reason: "Available", mounts: [] });
    if (url === "/api/v1/plan") return Response.json(rawPlan);
    if (url === "/api/v1/jobs" && init?.method === "POST") return Response.json({ spec: {}, state: {} }, { status: 201 });
    if (url === "/api/v1/resolve/timeline/rates") {
      const ids: string[] = JSON.parse(String(init?.body)).clip_ids;
      return Response.json({ clips: ids.map(id => ({ id, name: `${id}.mcraw`, rate: 24, usable: true, nearest: 24 })), message: null });
    }
    return library(input, init);
  });
}






it("opens a native DRB for virtual RAW media without scripting or physical export", async () => {
  engineForSends();
  const base = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url = String(input);
    if (url === "/api/v1/mounts" && init?.method === "POST") {
      posted.push({ url, body: JSON.parse(String(init?.body)) });
      return Response.json({ id: "mounted-a", source: "/clips/a.mcraw", path: "/mounted/a", frames: 418, active: true, source_available: true, stamp: [1, 2, 3, 4] }, { status: 201 });
    }
    if (url === "/api/v1/resolve/mounts/send") {
      posted.push({ url, body: JSON.parse(String(init?.body)) });
      return Response.json({ archive: "/state/send/test.drb", launched: true, results: [{ id: "mount-mounted-a", added: false, waiting: false, message: "Opened /state/send/test.drb. Complete the bin import in Resolve." }] });
    }
    return base(input, init);
  });
  render(Library);
  const user = userEvent.setup();
  await user.click(await card("a.mcraw"));
  await user.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
  await user.click(screen.getByRole("menuitem", { name: "Send to Resolve (Media Bin)" }));
  expect(screen.queryByRole("region", { name: "Send to Resolve (Media Bin)" })).toBeNull();
  expect(await within(await card("a.mcraw")).findByText(/Sent to Resolve/)).toBeInTheDocument();
  expect(screen.getByRole("status", { name: "Send activity" })).toHaveTextContent("1 clip sent to Resolve");
  expect(screen.getByText(/The handoff was dispatched/)).not.toBeVisible();
  expect(posted).toEqual([
    { url: "/api/v1/mounts", body: { clip_id: "a" } },
    { url: "/api/v1/resolve/mounts/send", body: { mounts: ["mounted-a"], mode: "bin", gyro: false, unchecked_gyro: false, rolling_shutter: false, progress: expect.stringMatching(/^[A-Za-z0-9-]+$/) } },
  ]);
});

it("keeps physical export available when virtual media is unavailable", async () => {
  engineForSends();
  const base = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, init) => String(input) === "/api/v1/mounts"
    ? Response.json({ available: false, reason: "FUSE unavailable; export CinemaDNG from Export", mounts: [] })
    : base(input, init));
  render(Library);
  const user = userEvent.setup();
  const option = await card("a.mcraw");
  expect(within(option).queryByRole("button", { name: "Export to Resolve" })).toBeNull();
  await user.click(option);
  await user.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
  await user.click(screen.getByRole("menuitem", { name: "Send to Resolve (Media Bin)" }));
  const activity = await screen.findByRole("status", { name: "Send activity" });
  expect(activity).toHaveTextContent("Could not send to Resolve (Media Bin)");
  expect(await screen.findByText(/FUSE unavailable/)).toBeVisible();
  expect(posted).toEqual([]);
});


it("shows detailed rows beside the selected clip's preview", async () => {
  listed[0] = clip("a", { frame_count: 436, measured_fps: 24.002, focal_length_mm: 25.8, aperture: 1.7, iso: 320, exposure_time_ns: 10000000, sensor_bits: 14, white_level: 16380, black_levels: [64, 64, 64, 64], missing_frames: 2 });
  render(Library);
  const row = await card("a.mcraw");
  expect(within(row).getByText(/25.8 mm \(35 mm eq.\)/)).toBeInTheDocument();
  expect(within(row).getByText(/ISO 320/)).toBeInTheDocument();
  expect(within(row).getByText("Frames:").parentElement).toHaveTextContent("Frames: 436");
  expect(within(row).queryByText(/14-bit/)).toBeNull();
  await userEvent.setup().click(row);
  expect(await screen.findByRole("region", { name: "Clip preview" })).toHaveTextContent("Preview a");
});

it("moves the selected clip's preview to where the clips are, and back", async () => {
  render(Library);
  await userEvent.setup().click(await card("a.mcraw"));
  const preview = await screen.findByRole("region", { name: "Clip preview" });
  const content = screen.getByRole("listbox", { name: "Clips" }).closest(".content")!;
  await fireEvent.click(within(preview).getByRole("button", { name: "Move the preview to the clip list" }));
  expect(content).toHaveClass("docked");
  expect(localStorage.getItem("mcrawsome.preview-docked")).toBe("1");
  expect(screen.getByRole("region", { name: "Clip preview" })).toBe(preview); // moved, not opened again
  await fireEvent.click(within(preview).getByRole("button", { name: "Move the preview back" }));
  expect(content).not.toHaveClass("docked");
  expect(localStorage.getItem("mcrawsome.preview-docked")).toBe("0");
});

it("opens the clip context menu with separate export and virtual send actions", async () => {
  engineForSends();
  render(Library);
  const row = await card("a.mcraw");
  row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 40, clientY: 50 }));
  const menu = await screen.findByRole("menu", { name: "Clip actions" });
  expect(within(menu).queryByRole("menuitem", { name: "Export to Resolve" })).toBeNull();
  await userEvent.setup().click(within(menu).getByRole("menuitem", { name: "Send to Resolve (Media Bin)" }));
  expect(await screen.findByRole("status", { name: "Send activity" })).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Send to Resolve (Media Bin)" })).toBeNull();
  expect(screen.queryByRole("menu")).toBeNull();
});

it("shows mounted status on the row and its path in the right panel", async () => {
  engineForSends();
  const base = vi.mocked(fetch).getMockImplementation()!;
  let active = true;
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input) === "/api/v1/mounts") return Response.json({ available: true, reason: "Available", mounts: [{ id: "mounted-a", source: "/clips/a.mcraw", path: "/home/test/Mounts/a", stamp: [1, 2, 3, 4], frames: 436, active, serving: active, source_available: true }] });
    if (String(input) === "/api/v1/mounts/mounted-a/unmount") { active = false; return Response.json({ active: false }); }
    return base(input, init);
  });
  render(Library);
  const row = await card("a.mcraw");
  expect(await within(row).findByText("Mounted")).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Mounted media" })).toBeNull();
  const user = userEvent.setup();
  await user.click(row);
  await userEvent.setup().click(screen.getByRole("tab", { name: "Clip info" }));
  let panel = await screen.findByRole("complementary", { name: "Clip details" });
  await user.click(screen.getByRole("tab", { name: "Media" }));
  panel = await screen.findByRole("complementary", { name: "Clip details" });
  expect(await within(panel).findByText("/home/test/Mounts/a")).toBeInTheDocument();
  await user.click(within(panel).getByRole("button", { name: "Unmount" }));
  await waitFor(() => expect(within(row).queryByText("Mounted")).toBeNull());
});

it("lets Space reach the video transport when the selected clip row has focus", async () => {
  render(Library);
  const row = await card("a.mcraw");
  await userEvent.setup().click(row);
  const event = new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true, cancelable: true });
  row.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(false); // The real preview transport handles the event at window.
});

it("assembles a paired timeline in the Library sort order", async () => {
  engineForSends();
  const base = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url = String(input);
    if (url === "/api/v1/mounts" && init?.method === "POST") {
      const id = JSON.parse(String(init.body)).clip_id;
      return Response.json({ id: `mounted-${id}` }, { status: 201 });
    }
    if (url === "/api/v1/resolve/mounts/send") {
      posted.push({ url, body: JSON.parse(String(init?.body)) });
      return Response.json({ archive: "/state/timeline.drt", launched: true, results: ["a", "ccc"].map(id => ({ id: `mount-mounted-${id}`, message: "Opened" })) });
    }
    return base(input, init);
  });
  render(Library);
  await card("a.mcraw");
  const user = userEvent.setup();
  await user.click(screen.getByRole("checkbox", { name: "Select clips in this view" }));
  expect(screen.getByRole("toolbar", { name: "Selected clips" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
  await user.click(screen.getByRole("menuitem", { name: "Send to Resolve (Timeline)" }));
  await waitFor(() => expect(posted.at(-1)?.body).toEqual({ mounts: ["mounted-ccc", "mounted-a"], mode: "timeline", gyro: false, unchecked_gyro: false, rolling_shutter: false, progress: expect.stringMatching(/^[A-Za-z0-9-]+$/) }));
});

function timelineRates(odd: Record<string, { rate: number; nearest: number | null }>): { saved: string[] } {
  engineForSends();
  const base = vi.mocked(fetch).getMockImplementation()!;
  const saved: string[] = [];
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url = String(input);
    if (url === "/api/v1/resolve/timeline/rates") {
      const ids: string[] = JSON.parse(String(init?.body)).clip_ids;
      posted.push({ url, body: { clip_ids: ids } });
      const clips = ids.map(id => odd[id] && !saved.includes(id) ? { id, name: `${id}.mcraw`, rate: odd[id].rate, usable: false, nearest: odd[id].nearest }
        : { id, name: `${id}.mcraw`, rate: 24, usable: true, nearest: 24 });
      return Response.json({ clips, message: clips.some(item => !item.usable) ? "Resolve timelines need a whole number of frames per second" : null });
    }
    const recipe = /^\/api\/v1\/clips\/([^/]+)\/recipe$/.exec(url)?.[1];
    if (recipe && init?.method === "PUT") {
      const body = JSON.parse(String(init.body));
      posted.push({ url, body });
      if (body.values.develop?.mount_frame_rate === "integer") saved.push(recipe);
      return Response.json({ values: body.values, version: "2" });
    }
    if (recipe) return Response.json({ layer: { values: { develop: { rotation: 90 } }, version: "1" }, resolved: {}, inherited: {} });
    if (url === "/api/v1/mounts" && init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      posted.push({ url, body });
      return Response.json({ id: `mounted-${body.clip_id}` }, { status: 201 });
    }
    if (url === "/api/v1/resolve/mounts/send") {
      posted.push({ url, body: JSON.parse(String(init?.body)) });
      return Response.json({ archive: "/state/timeline.drt", launched: true, results: ["a", "ccc"].map(id => ({ id: `mount-mounted-${id}`, message: "Opened" })) });
    }
    return base(input, init);
  });
  return { saved };
}

async function sendTimeline(): Promise<ReturnType<typeof userEvent.setup>> {
  render(Library);
  await card("a.mcraw");
  const user = userEvent.setup();
  await user.click(screen.getByRole("checkbox", { name: "Select clips in this view" }));
  await user.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
  await user.click(screen.getByRole("menuitem", { name: "Send to Resolve (Timeline)" }));
  return user;
}

it("names a clip at a rate a timeline cannot take before mounting anything, and sends once it mounts at a whole rate", async () => {
  const { saved } = timelineRates({ ccc: { rate: 23.792, nearest: 24 } });
  const user = await sendTimeline();
  const panel = await screen.findByRole("region", { name: "Frame rates for the timeline" });
  expect(panel).toHaveTextContent("ccc.mcraw");
  expect(panel).toHaveTextContent("23.792 fps");
  expect(panel).toHaveTextContent("24 fps");
  expect(panel).not.toHaveTextContent("a.mcraw");
  expect(posted.filter(item => item.url === "/api/v1/mounts")).toHaveLength(0);
  const loads = vi.mocked(fetch).mock.calls.filter(([url]) => url === "/api/v1/library").length;
  await user.click(within(panel).getByRole("button", { name: "Use 24 fps and send" }));
  await waitFor(() => expect(posted.at(-1)?.url).toBe("/api/v1/resolve/mounts/send"));
  expect(saved).toEqual(["ccc"]);
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => url === "/api/v1/library").length).toBeGreaterThan(loads);
  // Only the clip's Mounted frame rate changes; its other saved settings stay.
  expect(posted.find(item => item.url === "/api/v1/clips/ccc/recipe")?.body).toEqual({ values: { develop: { rotation: 90, mount_frame_rate: "integer" } }, version: "1" });
  expect(posted.at(-1)?.body).toMatchObject({ mounts: ["mounted-ccc", "mounted-a"], mode: "timeline" });
  expect(screen.queryByRole("region", { name: "Frame rates for the timeline" })).toBeNull();
});

it("leaves the clips as they were when the frame rate change is cancelled", async () => {
  timelineRates({ ccc: { rate: 23.792, nearest: 24 }, a: { rate: 29.97, nearest: 30 } });
  const user = await sendTimeline();
  const panel = await screen.findByRole("region", { name: "Frame rates for the timeline" });
  expect(panel).toHaveTextContent("29.97 fps");
  await user.click(within(panel).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("region", { name: "Frame rates for the timeline" })).toBeNull();
  expect(posted.map(item => item.url)).toEqual(["/api/v1/resolve/timeline/rates"]);
});

it("offers no rate change when no whole rate a timeline takes would help", async () => {
  timelineRates({ ccc: { rate: 120, nearest: null } });
  await sendTimeline();
  const panel = await screen.findByRole("region", { name: "Frame rates for the timeline" });
  expect(panel).toHaveTextContent("120 fps");
  expect(panel).toHaveTextContent("Media Bin");
  expect(within(panel).queryByRole("button", { name: /and send$/ })).toBeNull();
});

it("waits for the latest details to save before sending and keeps failed drafts", async () => {
  engineForSends();
  const base = vi.mocked(fetch).getMockImplementation()!;
  let finish: (response: Response) => void = () => {};
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input).endsWith("/annotations") && init?.method === "PUT") {
      return new Promise<Response>(resolve => { finish = resolve; });
    }
    return base(input, init);
  });
  render(Library);
  const user = userEvent.setup();
  await user.click(await card("a.mcraw"));
  await user.click(screen.getByRole("tab", { name: "Notes" }));
  const title = await screen.findByRole("textbox", { name: "Title" });
  await waitFor(() => expect(title).toBeEnabled());
  await user.type(title, "New name");
  expect(screen.getByRole("toolbar", { name: "Selected clips" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
  await user.click(screen.getByRole("menuitem", { name: "Send to Resolve (Media Bin)" }));
  expect(posted).toHaveLength(0);
  finish(Response.json({ error: { code: "recipe-changed", message: "Details changed elsewhere" } }, { status: 409 }));
  await screen.findByText("Could not save clip details. Resolve was not sent any clips. Check the clip details editor and try again.");
  expect(posted).toHaveLength(0);
  expect(title).toHaveValue("New name");
  await user.click(screen.getByRole("button", { name: "Discard edits and reload" }));
  await waitFor(() => expect(title).toHaveValue(""));
  await user.type(title, "Ready name");
  await user.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
  await user.click(screen.getByRole("menuitem", { name: "Send to Resolve (Media Bin)" }));
  expect(posted).toHaveLength(0);
  finish(Response.json({ values: { title: "Ready name" }, version: "saved", storage: "sidecar", message: "Saved", document: {} }));
  await waitFor(() => expect(posted.some(item => item.url === "/api/v1/resolve/mounts/send")).toBe(true));
  expect(title).toHaveValue("Ready name");
});

it("unmounts all selected mounts from the context menu", async () => {
  const base = vi.mocked(fetch).getMockImplementation()!;
  const unmounted: string[] = [];
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url = String(input);
    if (url === "/api/v1/mounts") return Response.json({ available: true, reason: "Available", mounts: ["a", "ccc"].map(id => ({ id, source: `/clips/${id}.mcraw`, path: `/mounts/${id}`, active: !unmounted.includes(id), serving: true, source_available: true, stamp: [1,2,3,4] })) });
    if (url.endsWith("/unmount")) { unmounted.push(url.split("/").at(-2)!); return Response.json({}); }
    return base(input, init);
  });
  render(Library);
  const user = userEvent.setup();
  for (const name of ["a.mcraw", "ccc.mcraw"]) await user.click(await screen.findByRole("checkbox", { name: `Select ${name}` }));
  (await card("a.mcraw")).dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
  await user.click(await screen.findByRole("menuitem", { name: "Unmount" }));
  await waitFor(() => expect(unmounted).toEqual(["a", "ccc"]));
});

it("cycles the active preview through selected clips without changing selection", async () => {
  render(Library);
  const user = userEvent.setup();
  for (const name of ["a.mcraw", "ccc.mcraw"]) await user.click(await screen.findByRole("checkbox", { name: `Select ${name}` }));
  const chosen = [...get(selection)];
  const nav = await screen.findByRole("navigation", { name: "Selected clip preview" });
  expect(nav).toHaveTextContent("1 of 2 selected");
  expect(within(screen.getByRole("region", { name: "Clip preview and information" })).getByRole("navigation", { name: "Selected clip preview" })).toBe(nav);
  expect(screen.getByRole("option", { name: "ccc.mcraw" })).toHaveAttribute("data-active", "true");
  await user.click(within(nav).getByRole("button", { name: "Next selected clip" }));
  expect(get(selection)).toEqual(chosen);
  expect(nav).toHaveTextContent("2 of 2 selected");
  expect(screen.getByRole("option", { name: "a.mcraw" })).toHaveAttribute("data-active", "true");
  expect(screen.getByRole("complementary", { name: "Clip workspace" })).toHaveTextContent("a.mcraw");
  await user.click(within(nav).getByRole("button", { name: "Previous selected clip" }));
  expect(nav).toHaveTextContent("1 of 2 selected");
  expect(get(selection)).toEqual(chosen);
});


it("updates sent media use flags from events and protects the Unmount button", async () => {
  const base = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (url, init) => String(url) === "/api/v1/mounts"
    ? Response.json({ available: true, reason: "Available", mounts: [{ id: "m-a", source: "/clips/a.mcraw", path: "/mounts/a", active: true, serving: true, source_available: true, sent: true, in_use: false }] })
    : base(url, init));
  render(Library);
  const row = await card("a.mcraw");
  await waitFor(() => expect(within(row).getByText("Idle")).toBeInTheDocument());
  await userEvent.click(within(row).getByRole("checkbox", { name: "Select a.mcraw" }));
  await userEvent.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
  expect(screen.getByRole("menuitem", { name: "Unmount" })).toBeEnabled();
  const source = FakeEventSource.instances.at(-1)!;
  source.send("media-use", { "m-a": true });
  await waitFor(() => expect(within(row).getByText("In use")).toBeInTheDocument());
  expect(within(row).queryByRole("button", { name: "Unmount" })).toBeNull();
  expect(screen.getByRole("menuitem", { name: "Unmount" })).toBeDisabled();
  source.send("media-use", { "m-a": false });
  await waitFor(() => expect(within(row).getByText("Idle")).toBeInTheDocument());
  expect(screen.getByRole("menuitem", { name: "Unmount" })).toBeEnabled();
});


it("uses a mixed-state view checkbox without clearing hidden selections and keeps actions in one menu", async () => {
  render(Library);await card("a.mcraw");
  const user=userEvent.setup();
  const toggle=screen.getByRole("checkbox",{name:"Select clips in this view"});
  expect(screen.queryByRole("button",{name:"Select all visible"})).toBeNull();
  expect(screen.queryByRole("button",{name:"Clear selection"})).toBeNull();
  await user.click(toggle);expect(get(selection)).toEqual(expect.arrayContaining(["a","bb","ccc"]));
  await user.click(screen.getByRole("button",{name:"RAW"}));
  await user.click(toggle);expect(get(selection)).toEqual(["bb"]);
  await user.click(screen.getByRole("checkbox",{name:"Select a.mcraw"}));expect(toggle).toBePartiallyChecked();
  await user.click(screen.getByRole("button",{name:"More actions for a.mcraw"}));
  expect(screen.queryByRole("menuitem",{name:"Export..."})).toBeNull();
  expect(screen.queryByRole("button",{name:"Develop selected"})).toBeNull();
});

it("defaults to temporary references without a library and keeps dropped files out of folder settings", async () => {
  sendActivity.set({ target: "resolve", targetLabel: "Resolve", busy: false, entries: [{ id: "a", name: "a.mcraw", target: "resolve", targetLabel: "Resolve", state: "sent", message: "Sent", archive: "/sent.drb", at: "2026-10-19T00:00:00Z" }] });
  settings.set({ ...saved, folders: [] } as never);
  const base = vi.mocked(fetch).getMockImplementation()!;
  let opened: Clip[] = [];
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url) === "/api/v1/opened-clips") {
      if (init?.method === "POST") { expect(JSON.parse(String(init.body))).toEqual({ paths: ["/outside/dropped.mcraw"] }); opened = [clip("drop", { path: "/outside/dropped.mcraw", name: "dropped.mcraw" })]; }
      return Response.json({ clips: opened, missing_folders: [] });
    }
    return base(url, init);
  });
  render(Library);
  expect(await screen.findByRole("heading", { name: "Open clips" })).toBeInTheDocument();
  expect(screen.getByText(/not added to your library/i)).toBeInTheDocument();
  window.dispatchEvent(new CustomEvent("mcrawsome-files-dropped", { detail: ["/outside/dropped.mcraw"] }));
  await card("dropped.mcraw");
  expect(screen.queryByRole("status", { name: "Send activity" })).toBeNull();
  expect(screen.queryByText("1 clip sent to Resolve")).toBeNull();
  expect(screen.queryByRole("option", { name: "a.mcraw" })).toBeNull();
  expect(get(settings)?.folders).toEqual([]);
  expect(vi.mocked(fetch).mock.calls.some(([url, init]) => String(url).endsWith("/settings") && init?.method === "PUT")).toBe(false);
  await userEvent.click(screen.getByRole("button", { name: "Library folders" }));
  expect(await card("a.mcraw")).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "dropped.mcraw" })).toBeNull();
});

it("replaces the empty drop square with a top hint and accepts further drops across the view", async () => {
  settings.set({ ...saved, folders: [] } as never);
  const base = vi.mocked(fetch).getMockImplementation()!;
  let opened: Clip[] = [];
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url) !== "/api/v1/opened-clips") return base(url, init);
    if (init?.method === "DELETE") opened = [];
    if (init?.method === "POST") {
      const paths = JSON.parse(String(init.body)).paths as string[];
      opened.push(...paths.map((path, index) => clip(`drop-${opened.length + index}`, { path, name: path.split("/").at(-1)! })));
    }
    return Response.json({ clips: opened, missing_folders: [] });
  });
  render(Library);
  const view = await screen.findByRole("region", { name: "Open clips" });
  expect(within(view).getByRole("heading", { name: "Drop clips here" })).toBeInTheDocument();
  const transfer = (uri: string) => ({ getData: () => uri });
  await fireEvent.drop(view, { dataTransfer: transfer("file:///outside/first.mcraw") });
  await card("first.mcraw");
  expect(screen.queryByRole("heading", { name: "Drop clips here" })).toBeNull();
  expect(within(view).getByText("Drop clips here to add more")).toBeInTheDocument();
  await fireEvent.drop(screen.getByRole("listbox", { name: "Clips" }), { dataTransfer: transfer("file:///outside/second.mcraw") });
  await card("second.mcraw");
  expect(await card("first.mcraw")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Clear list" }));
  expect(await screen.findByRole("heading", { name: "Drop clips here" })).toBeInTheDocument();
  expect(screen.queryByText("Drop clips here to add more")).toBeNull();
  expect(get(settings)?.folders).toEqual([]);
});

it("temporarily shows the drop box for file drags and restores the same selected preview", async () => {
  mediaView.set("opened");
  const base = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (url, init) => String(url) === "/api/v1/opened-clips"
    ? Response.json({ clips: listed.slice(0, 3), missing_folders: [] }) : base(url, init));
  render(Library);
  const row = await card("a.mcraw");
  await userEvent.click(row);
  const preview = screen.getByRole("region", { name: "Clip preview" });
  await fireEvent.dragEnter(row, { dataTransfer: { types: ["text/plain"] } });
  expect(screen.queryByRole("heading", { name: "Drop clips here" })).toBeNull();
  const dataTransfer = { types: ["Files"], getData: () => "" };
  await fireEvent.dragEnter(window, { dataTransfer });
  expect(screen.getByRole("heading", { name: "Drop clips here" })).toBeVisible();
  expect(screen.queryByRole("listbox", { name: "Clips" })).toBeNull();
  await fireEvent.dragEnter(screen.getByRole("heading", { name: "Open clips" }), { dataTransfer });
  await fireEvent.dragLeave(window, { dataTransfer });
  expect(screen.getByRole("heading", { name: "Drop clips here" })).toBeVisible();
  await fireEvent.dragLeave(window, { dataTransfer });
  expect(screen.queryByRole("heading", { name: "Drop clips here" })).toBeNull();
  expect(screen.getByRole("region", { name: "Clip preview" })).toBe(preview);
  expect(get(selection)).toEqual(["a"]);
  await fireEvent.dragEnter(window, { dataTransfer });
  await fireEvent.drop(window, { dataTransfer });
  expect(screen.queryByRole("heading", { name: "Drop clips here" })).toBeNull();
  expect(screen.getByRole("region", { name: "Clip preview" })).toBe(preview);
});

it("removes one or multiple selected open clips through the context menu", async () => {
  mediaView.set("opened");
  let opened = listed.slice(0, 3);
  const base = vi.mocked(fetch).getMockImplementation()!;
  const removed: string[][] = [];
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url) === "/api/v1/opened-clips/remove") {
      const paths = JSON.parse(String(init?.body)).paths as string[];
      removed.push(paths);
      opened = opened.filter(clip => !paths.includes(String(clip.path)));
      return Response.json({ clips: opened, missing_folders: [] });
    }
    if (String(url) === "/api/v1/opened-clips") return Response.json({ clips: opened, missing_folders: [] });
    return base(url, init);
  });
  render(Library);
  await card("a.mcraw");
  const user = userEvent.setup();
  await user.click(screen.getByRole("checkbox", { name: "Select a.mcraw" }));
  await user.click(screen.getByRole("checkbox", { name: "Select bb.mp4" }));
  await user.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
  await user.click(screen.getByRole("menuitem", { name: "Remove from session" }));
  await waitFor(() => expect(screen.queryByRole("option", { name: "a.mcraw" })).toBeNull());
  expect(screen.queryByRole("option", { name: "bb.mp4" })).toBeNull();
  expect(await card("ccc.mcraw")).toBeInTheDocument();
  expect(get(selection)).toEqual([]);
  expect(removed).toEqual([["/clips/a.mcraw", "/clips/bb.mp4", "/clips/bb.mcraw"]]);
  await user.click(screen.getByRole("button", { name: "More actions for ccc.mcraw" }));
  await user.click(screen.getByRole("menuitem", { name: "Remove from session" }));
  expect(await screen.findByRole("heading", { name: "Drop clips here" })).toBeVisible();
  expect(removed[1]).toEqual(["/clips/ccc.mcraw"]);
});

it("mounts from the card context menu, refreshes its state, and never launches Resolve", async () => {
  const base = vi.mocked(fetch).getMockImplementation()!;
  let mounted = false;
  const requests: string[] = [];
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url = String(input);
    if (url === "/api/v1/mounts" && init?.method === "POST") {
      requests.push(url); mounted = true;
      expect(JSON.parse(String(init.body))).toEqual({ clip_id: "a" });
      return Response.json({ id: "m-a" }, { status: 201 });
    }
    if (url === "/api/v1/mounts") return Response.json({ available: true, mounts: mounted ? [{ id: "m-a", source: "/clips/a.mcraw", path: "/mounts/a", active: true, serving: true, source_available: true }] : [] });
    if (url.includes("resolve")) requests.push(url);
    return base(input, init);
  });
  render(Library);
  const row = await card("a.mcraw");
  const user = userEvent.setup();
  await user.click(within(row).getByRole("button", { name: "More actions for a.mcraw" }));
  await user.click(await screen.findByRole("menuitem", { name: "Mount" }));
  expect(await within(row).findByText("Mounted")).toBeVisible();
  expect(requests).toEqual(["/api/v1/mounts"]);
  expect(get(selection)).toEqual(["a"]);
});

it("opens the mounted virtual folder rather than the recording's source folder", async () => {
  const base = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, init) => String(input) === "/api/v1/mounts"
    ? Response.json({ available: true, mounts: [{ id: "m-a", source: "/clips/a.mcraw", path: "/mounts/current-a", active: true, serving: true, source_available: true }] }) : base(input, init));
  const open_folder = vi.fn(async () => {});
  window.pywebview = { api: { open_folder, choose_folder: vi.fn(), restart_engine: vi.fn() } };
  try {
    render(Library);
    const row = await card("a.mcraw");
    await within(row).findByText("Mounted");
    row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    await userEvent.setup().click(await screen.findByRole("menuitem", { name: "Open Mount Folder" }));
    expect(open_folder).toHaveBeenCalledExactlyOnceWith("/mounts/current-a");
  } finally { delete window.pywebview; }
});

it("names the clip and its save status on every tab, and gathers Info, Analytics and Metadata in Clip info", async () => {
  render(Library);
  const user = userEvent.setup();
  await user.click(await card("a.mcraw"));
  const workspace = screen.getByRole("complementary", { name: "Clip workspace" });
  expect(within(workspace).getAllByRole("tab").map(tab => tab.textContent)).toEqual(["Resolve Settings", "Clip info", "Notes", "Media"]);
  for (const name of ["Resolve Settings", "Clip info", "Notes", "Media"]) {
    await user.click(within(workspace).getByRole("tab", { name }));
    expect(within(workspace).getByText("a.mcraw", { selector: ".name" })).toHaveAttribute("title", listed[0]!.path);
    expect(within(workspace).getByRole("status", { name: "Resolve settings save status" })).toHaveTextContent("Saved");
  }
  await user.click(within(workspace).getByRole("tab", { name: "Clip info" }));
  expect(within(workspace).getByRole("button", { name: /^Info/ })).toHaveAttribute("aria-expanded", "true");
  expect(await within(workspace).findByRole("complementary", { name: "Clip details" })).toBeInTheDocument();
  for (const name of [/^Analytics/, /^Metadata/]) expect(within(workspace).getByRole("button", { name })).toHaveAttribute("aria-expanded", "false");
  await user.click(within(workspace).getByRole("button", { name: /^Metadata/ }));
  expect(within(workspace).getByRole("complementary", { name: "Clip metadata" })).toBeInTheDocument();
});

it("keeps one preview and its controls alive while switching accessible inspector tabs", async () => {
  listed[0] = clip("a", { gyro: true, gps: true });
  render(Library);
  const user = userEvent.setup();
  await user.click(await card("a.mcraw"));
  const preview = screen.getByRole("region", { name: "Clip preview" });
  const controls = screen.getByText("Development controls a");
  const develop = screen.getByRole("tab", { name: "Resolve Settings", selected: true });
  await user.click(screen.getByRole("tab", { name: "Clip info" }));
  expect(controls).not.toBeVisible();
  expect(screen.getByRole("region", { name: "Clip preview" })).toBe(preview);
  await user.keyboard("{End}");
  expect(screen.getByRole("tab", { name: "Media", selected: true })).toHaveFocus();
  expect(screen.getByRole("tabpanel")).toHaveAccessibleName("Media");
  await user.keyboard("{Home}");
  expect(develop).toHaveFocus();
  expect(controls).toBeVisible();
  expect(screen.getAllByRole("region", { name: "Clip preview" })).toHaveLength(1);
});

it("keeps Notes selected and focuses the new title after Save and next", async () => {
  render(Library);
  const user = userEvent.setup();
  for (const name of ["a.mcraw", "ccc.mcraw"]) await user.click(await screen.findByRole("checkbox", { name: `Select ${name}` }));
  await user.click(screen.getByRole("tab", { name: "Notes" }));
  const first = await screen.findByRole("textbox", { name: "Title" });
  await waitFor(() => expect(first).toBeEnabled());
  expect(first).toHaveFocus();
  await user.click(screen.getByRole("button", { name: "Save details and next clip" }));
  await waitFor(() => expect(screen.getByRole("region", { name: "Clip details editor" })).toHaveTextContent("a.mcraw"));
  expect(screen.getByRole("tab", { name: "Notes", selected: true })).toBeVisible();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Title" })).toHaveFocus());
  expect(screen.getByRole("textbox", { name: "Title" })).not.toBe(first);
});

it("opens icon menu from a selected card's dots, preserves the selection and restores focus", async () => {
  render(Library);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("checkbox", { name: "Select a.mcraw" }));
  await user.click(screen.getByRole("checkbox", { name: "Select ccc.mcraw" }));
  const button = screen.getByRole("button", { name: "More actions for a.mcraw" });
  await user.click(button);
  const menu = await screen.findByRole("menu", { name: "Clip actions" });
  expect(menu).toHaveTextContent("2 selected");
  expect(get(selection)).toEqual(["a", "ccc"]);
  expect(button).toHaveAttribute("aria-expanded", "true");
  for (const item of within(menu).getAllByRole("menuitem")) expect(item.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  await user.keyboard("{Escape}");
  expect(button).toHaveFocus();
  expect(button).toHaveAttribute("aria-expanded", "false");
});

it("combines detailed facets with name and RAW filters and keeps hidden selections", async () => {
  listed[0] = clip("a", { iso: 100, rating: 5 });
  listed[2] = clip("ccc", { iso: 800, rating: 2 });
  render(Library);
  const user = userEvent.setup();
  await user.click(await card("ccc.mcraw"));
  await user.click(screen.getByRole("button", { name: "Filter" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "Filter ISO" }), "100");
  expect(cards()).toEqual(["a.mcraw"]);
  expect(screen.getByText("1 outside this view")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "RAW" }));
  await user.type(screen.getByRole("searchbox", { name: "Search names and details" }), "ccc");
  expect(screen.getByText("No clips match.")).toBeInTheDocument();
  expect(get(selection)).toEqual(["ccc"]);
});

it("unmounts all active media including clips outside the selection and shows failures without a success log", async () => {
  const base = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input) === "/api/v1/mounts") return Response.json({ available: true, reason: "Available", mounts: [{ id: "mounted-a", source: "/clips/a.mcraw", active: true }] });
    if (String(input) === "/api/v1/mounts/unmount-all") return Response.json({ results: [{ id: "a", source: "/clips/a.mcraw", ok: true, message: "Unmounted" }, { id: "outside", source: "/other/outside.mcraw", ok: false, message: "Media is in use; close its open files in the receiving application before unmounting" }] });
    return base(input, init);
  });
  render(Library);
  await card("a.mcraw");
  expect(get(selection)).toEqual([]);
  await userEvent.setup().click(await screen.findByRole("button", { name: "Unmount All" }));
  expect(await screen.findByRole("alert", { name: "" })).toHaveTextContent("/other/outside.mcraw: Media is in use");
  expect(screen.queryByText("/clips/a.mcraw: Unmounted")).toBeNull();
});

it("keeps session references when pending clip edits cannot be saved before clearing the list", async () => {
  const base = vi.mocked(fetch).getMockImplementation()!;
  let cleared = 0;
  const session = { name: "Edit", path: null, portable: false, workspace: { paths: ["/clips/a.mcraw"], selection: ["a"], focused: "a", search: "", kind: "all", sort: "name", facets: {} }, clips: [listed[0]], missing: [], recent: [], problem: null };
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input) === "/api/v1/session") {
      if (init?.method === "PUT" && JSON.parse(String(init.body)).paths.length === 0) {
        cleared += 1;
        return Response.json({ ...session, workspace: { ...session.workspace, paths: [], selection: [], focused: null }, clips: [] });
      }
      return Response.json(session);
    }
    return base(input, init);
  });
  render(Library);
  await waitFor(() => expect(get(selection)).toEqual(["a"]));
  const { annotationsFor } = await import("../src/lib/annotations");
  vi.spyOn(annotationsFor("a"), "flush").mockResolvedValue(false);
  await userEvent.setup().click(screen.getByRole("button", { name: "Clear list" }));
  expect(cleared).toBe(0);
  expect(await card("a.mcraw")).toBeVisible();
});

it("keeps send and export in the selection menu and mounting in Media, without duplicate inspector buttons", async () => {
  render(Library);
  const row = await card("a.mcraw");
  expect(within(row).queryByRole("button", { name: /^(Mount|Unmount|Send to|Choose send)/ })).toBeNull();
  expect(within(row).queryByRole("switch")).toBeNull();
  const user = userEvent.setup();
  await user.click(within(row).getByRole("checkbox"));
  await user.click(screen.getByRole("button", { name: "More actions for a.mcraw" }));
  const menu = await screen.findByRole("menu", { name: "Clip actions" });
  expect(within(menu).getByRole("menuitem", { name: "Mount" })).toBeEnabled();
  expect(within(menu).getByRole("menuitem", { name: "Send to Resolve (Media Bin)" })).toBeEnabled();
  await user.keyboard("{Escape}");
  const inspector = screen.getByRole("complementary", { name: "Clip workspace" });
  expect(within(inspector).queryByRole("button", { name: "Send to Resolve (Media Bin)" })).toBeNull();
  expect(within(inspector).queryByRole("button", { name: "Export this clip" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Copy from Card" })).toBeNull();
  await user.click(within(inspector).getByRole("tab", { name: "Media" }));
  expect(within(inspector).getByRole("button", { name: "Mount with current settings" })).toBeEnabled();
});

it.each([true, false])("shows Mounting while preparation is pending and clears it on completion (success=%s)", async success => {
  const base = vi.mocked(fetch).getMockImplementation()!;
  let finish: (value: Response) => void = () => {};
  let mounted = false;
  const record = { id: "m-a", source: "/clips/a.mcraw", path: "/mounts/a", active: true, serving: true, source_available: true };
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input) === "/api/v1/mounts" && init?.method === "POST") return new Promise<Response>(resolve => finish = resolve);
    if (String(input) === "/api/v1/mounts") return Response.json({ available: true, mounts: mounted ? [record] : [] });
    return base(input, init);
  });
  render(Library);
  const row = await card("a.mcraw");
  const user = userEvent.setup();
  await user.click(within(row).getByRole("button", { name: "More actions for a.mcraw" }));
  await user.click(await screen.findByRole("menuitem", { name: "Mount" }));
  expect(await within(row).findByText("Mounting")).toHaveClass("warn");
  expect(within(row).queryByText("Unmounted")).toBeNull();
  expect(within(await card("ccc.mcraw")).queryByText("Mounting")).toBeNull();
  mounted = success;
  finish(success ? Response.json(record, { status: 201 }) : Response.json({ error: { code: "mount-failed", message: "Test mount failed" } }, { status: 422 }));
  expect(await within(row).findByText(success ? "Mounted" : "Unmounted")).toBeVisible();
  expect(within(row).queryByText("Mounting")).toBeNull();
});

it("filters recent clips by calendar range while preserving hidden selection and other filters", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 25, 12));
  try {
    listed[0] = clip("a", { captured: new Date(2026, 9, 25, 10).toISOString() });
    listed[1] = clip("bb", { kind: "log", name: "bb.mp4", captured: new Date(2026, 9, 20, 10).toISOString() });
    listed[2] = clip("ccc", { captured: new Date(2026, 9, 1, 10).toISOString() });
    listed[3] = { ...listed[3], captured: null };
    render(Library);
    const user = userEvent.setup();
    await user.click(await card("ccc.mcraw"));
    await user.click(screen.getByRole("button", { name: "Filter" }));
    const dates = screen.getByRole("combobox", { name: "Filter Date" });
    await user.selectOptions(dates, screen.getByRole("option", { name: "Today" }));
    expect(cards()).toEqual(["a.mcraw"]);
    expect(screen.getByText("1 outside this view")).toBeInTheDocument();
    await user.selectOptions(dates, screen.getByRole("option", { name: "This week" }));
    expect(cards()).toEqual(["a.mcraw", "bb.mp4"]);
    await user.selectOptions(dates, screen.getByRole("option", { name: "This month" }));
    expect(cards()).toEqual(["a.mcraw", "bb.mp4", "ccc.mcraw"]);
    await user.click(screen.getByRole("button", { name: "RAW" }));
    await user.type(screen.getByRole("searchbox", { name: "Search names and details" }), "ccc");
    expect(cards()).toEqual(["ccc.mcraw"]);
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(dates).toHaveValue("");
    expect(cards()).toEqual(["ccc.mcraw"]);
    expect(get(selection)).toEqual(["ccc"]);
  } finally { vi.useRealTimers(); }
});

it("restores a saved relative date filter as a range instead of a frozen date", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 25, 12));
  try {
    listed[0] = clip("a", { captured: new Date(2026, 9, 25, 10).toISOString() });
    listed[2] = clip("ccc", { captured: new Date(2026, 9, 1, 10).toISOString() });
    listed[3] = { ...listed[3], captured: null };
    const session = { name: "Recent", path: "/edit.mcrawsome-session.json", portable: false,
      workspace: { paths: ["/clips/a.mcraw", "/clips/ccc.mcraw"], selection: ["ccc"], focused: "ccc", search: "", kind: "all", sort: "captured", facets: { captured: "@this-week" } },
      clips: [listed[0], listed[2]], missing: [], recent: [], problem: null };
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (url, init) => String(url) === "/api/v1/session" ? Response.json(session) : base(url, init));
    render(Library);
    await screen.findByRole("heading", { name: "Open clips" });
    expect(cards()).toEqual(["a.mcraw"]);
    await userEvent.click(screen.getByRole("button", { name: "Filter (active)" }));
    expect(screen.getByRole("combobox", { name: "Filter Date" })).toHaveValue("@this-week");
    expect(screen.getByText("1 outside this view")).toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});

it("refreshes a relative date range when an open screen crosses midnight", async () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(new Date(2026, 9, 25, 23, 59, 59));
  try {
    listed = [clip("a", { captured: new Date(2026, 9, 25, 10).toISOString() })];
    render(Library);
    await card("a.mcraw");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Filter" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter Date" }), "@today");
    expect(cards()).toEqual(["a.mcraw"]);
    await vi.advanceTimersByTimeAsync(5000);
    expect(screen.getByText("No clips match.")).toBeInTheDocument();
  } finally { cleanup(); vi.useRealTimers(); }
});
