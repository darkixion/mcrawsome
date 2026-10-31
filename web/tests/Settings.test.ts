import { render, screen, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Settings from "../src/screens/Settings.svelte";

const saved = { quad_bayer_remosaic: true, folders: ["/clips"], destination: null, workers: null, keep_engine: false, remount_on_start: false, resolve_route: "auto", resolve_full_resolution: false, resolve_separate_audio: false, resolve_link_audio: true };
const doctor = { checks: [{ name: "FFmpeg", status: "ok", detail: "7.1", fix: null }] };
let getReply: unknown;
let putReply: { status: number; body: unknown };

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  getReply = saved;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/v1/settings" && init?.method === "PUT") return json(putReply.status, putReply.body);
      if (url === "/api/v1/settings") return json(200, getReply);
      if (url === "/api/v1/mounts") return json(200, { available: true, reason: "Available", mounts: [], restoration: { running: false, results: [] } });
      if (url === "/api/v1/doctor") return json(200, doctor);
      return json(404, { error: { code: "input-missing", message: "no such route" } });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function puts(): unknown[] {
  return vi
    .mocked(fetch)
    .mock.calls.filter(([, init]) => init?.method === "PUT")
    .map(([, init]) => JSON.parse(init?.body as string));
}

describe("Settings", () => {
  it("groups preferences and keeps diagnostics and mount records out of Settings", async () => {
    render(Settings);
    expect(await screen.findByText("/clips")).toBeInTheDocument();
    for (const heading of ["General", "DaVinci Resolve", "System", "Processing", "Debug"]) expect(screen.getByRole("heading", { name: heading })).toBeVisible();
    expect(screen.queryByRole("switch", { name: "Enable Send to Resolve" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "System check" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Sent media" })).toBeNull();
    expect(vi.mocked(fetch).mock.calls.some(([url]) => url === "/api/v1/doctor")).toBe(false);
  });

  it("saves an added folder through PUT /settings", async () => {
    putReply = { status: 200, body: { ...saved, folders: ["/clips", "/more"] } };
    const user = userEvent.setup();
    render(Settings);
    await screen.findByText("/clips");
    await user.type(screen.getByLabelText("Add a folder"), "/more");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(puts()).toEqual([{ ...saved, folders: ["/clips", "/more"] }]);
    expect(await screen.findByText("/more")).toBeInTheDocument();
    expect(screen.getByLabelText("Add a folder")).toHaveValue("");
  });

  it("shows the engine's 422 message beside the folder field", async () => {
    const message = "folders: /gone does not exist or is not a folder";
    putReply = { status: 422, body: { error: { code: "recipe-invalid", message, field: "folders" } } };
    const user = userEvent.setup();
    render(Settings);
    await screen.findByText("/clips");
    await user.type(screen.getByLabelText("Add a folder"), "/gone");
    await user.click(screen.getByRole("button", { name: "Add" }));
    const folders = screen.getByRole("group", { name: "Folders" });
    expect(await within(folders).findByText(message)).toBeInTheDocument();
    expect(within(folders).queryByText("/gone")).toBeNull(); // not added
    expect(screen.getByLabelText("Add a folder")).toHaveValue("/gone"); // kept to correct
  });

  it("saves the other settings as they change", async () => {
    putReply = { status: 200, body: { ...saved, keep_engine: true } };
    const user = userEvent.setup();
    render(Settings);
    await screen.findByText("/clips");
    await user.click(screen.getByRole("switch", { name: /Keep the engine running/ }));
    expect(puts()).toEqual([{ ...saved, keep_engine: true }]);
    expect(screen.queryByRole("switch", { name: "Enable Send to Resolve" })).toBeNull();
  });

  it("shows where mounts go when no folder is set, and never saves that as a setting", async () => {
    const user = userEvent.setup();
    getReply = { ...saved, mount_folder_default: "/home/me/Mounts/mcrawsome" };
    putReply = { status: 200, body: getReply };
    render(Settings);
    const folder = await screen.findByLabelText("Mount folder");
    expect(folder).toHaveAttribute("placeholder", "/home/me/Mounts/mcrawsome");
    await user.click(screen.getByRole("switch", { name: /Keep the engine running/ }));
    expect(puts()).toEqual([{ ...saved, keep_engine: true }]);
  });

  it("puts editor and desktop preferences in their own categories and removes restoration", async () => {
    render(Settings);
    const resolve = await screen.findByRole("region", { name: "DaVinci Resolve" });
    const general = screen.getByRole("region", { name: "General" });
    expect(within(general).getByLabelText("Mount folder")).toBeVisible();
    expect(within(resolve).queryByLabelText("Mount folder")).toBeNull();
    const system = screen.getByRole("region", { name: "System" });
    expect(within(system).getByRole("heading", { name: "File manager" })).toBeVisible();
    expect(within(system).getByRole("heading", { name: "File association" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Restore sent media" })).toBeNull();
    expect(screen.queryByLabelText("Restore sent media when the engine starts")).toBeNull();
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/mounts"))).toBe(false);
  });

  it("saves the off-by-default preview statistics toggle in Debug", async () => {
    putReply = { status: 200, body: { ...saved, show_preview_stats: true } };
    render(Settings);
    const toggle = await screen.findByRole("switch", { name: "Show preview performance statistics" });
    expect(toggle).not.toBeChecked();
    await userEvent.setup().click(toggle);
    expect(puts()).toEqual([{ ...saved, show_preview_stats: true }]);
    expect(screen.queryByLabelText("Render workers")).toBeNull();
  });

  it("refuses a folder already in the list without asking the engine", async () => {
    const user = userEvent.setup();
    render(Settings);
    await screen.findByText("/clips");
    await user.type(screen.getByLabelText("Add a folder"), "/clips/");
    await user.click(screen.getByRole("button", { name: "Add" }));
    const folders = screen.getByRole("group", { name: "Folders" });
    expect(await within(folders).findByText("/clips/ is already in the list")).toBeInTheDocument();
    expect(within(folders).getAllByText("/clips")).toHaveLength(1);
    expect(puts()).toEqual([]);
  });

  it("shows why the saved settings could not be read until they are saved again", async () => {
    const problem = "/home/me/.config/mcrawsome/interface.toml: Invalid value (at line 1, column 11)";
    getReply = { ...saved, folders: [], problem };
    putReply = { status: 200, body: { ...saved, keep_engine: true, folders: [], problem: null } };
    const user = userEvent.setup();
    render(Settings);
    expect(await screen.findByText(problem)).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: /Keep the engine running/ }));
    expect(puts()).toEqual([{ ...saved, folders: [], keep_engine: true }]); // no problem sent back
    expect(screen.queryByText(problem)).toBeNull();
  });
});

it("uses the folder dialog when the bridge arrives after rendering", async () => {
  putReply = { status: 200, body: { ...saved, folders: ["/clips", "/native"] } };
  render(Settings);
  await screen.findByText("/clips");
  const choose = vi.fn(async () => "/native");
  window.pywebview = { api: { choose_folder: choose, open_folder: vi.fn(), restart_engine: vi.fn() } };
  window.dispatchEvent(new Event("pywebviewready"));
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Choose folder" }));
  expect(choose).toHaveBeenCalledOnce();
  expect(screen.getByLabelText("Add a folder")).toHaveValue("/native");
  delete window.pywebview;
});



it("saves Quad Bayer Remosaic and explains automatic CFA selection", async () => {
  getReply = { ...saved, quad_bayer_remosaic: true };
  putReply = { status: 200, body: { ...saved, quad_bayer_remosaic: false } };
  render(Settings);
  const toggle = await screen.findByRole("switch", { name: "Quad Bayer Remosaic" });
  expect(toggle).toBeChecked();
  await userEvent.setup().click(toggle);
  expect(puts().at(-1)).toMatchObject({ quad_bayer_remosaic: false });
  expect(toggle).not.toBeChecked();
  expect(screen.queryByRole("tooltip")).toBeNull();
  await userEvent.setup().hover(screen.getByRole("button", { name: "About Quad Bayer Remosaic" }));
  expect(screen.getByRole("tooltip")).toHaveTextContent(/GBRG.*RGGB.*GRBG.*BGGR/);
});

it("retains the saved remosaic choice when saving fails", async () => {
  getReply = { ...saved, quad_bayer_remosaic: true };
  putReply = { status: 422, body: { error: { code: "recipe-invalid", message: "Could not save remosaic", field: "quad_bayer_remosaic" } } };
  render(Settings);
  const toggle = await screen.findByRole("switch", { name: "Quad Bayer Remosaic" });
  await userEvent.setup().click(toggle);
  expect(await screen.findByText("Could not save remosaic")).toBeVisible();
  expect(toggle).toBeChecked();
});

it("queues rapid changes so one preference cannot overwrite another", async () => {
  const original = fetch;
  const replies: ((value: Response) => void)[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    if (url === "/api/v1/settings" && init?.method === "PUT") return new Promise<Response>(resolve => replies.push(resolve));
    return original(url, init);
  }));
  render(Settings);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("switch", { name: "Quad Bayer Remosaic" }));
  await user.click(screen.getByRole("switch", { name: /Keep the engine running/ }));
  expect(puts()).toHaveLength(1);
  replies[0]!(json(200, { ...saved, quad_bayer_remosaic: false }));
  await vi.waitFor(() => expect(puts()).toHaveLength(2));
  expect(puts()[1]).toMatchObject({ quad_bayer_remosaic: false, keep_engine: true });
  replies[1]!(json(200, { ...saved, quad_bayer_remosaic: false, keep_engine: true }));
});

it("updates file association through the desktop bridge and allows retry after failure", async () => {
  const file_association = vi.fn(async () => false);
  const associate_files = vi.fn(async () => true);
  associate_files.mockRejectedValueOnce(new Error("Failed"));
  window.pywebview = { api: { file_association, associate_files, choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn() } };
  try {
    render(Settings);
    const button = await screen.findByRole("button", { name: "Use MCRAWsome to open .mcraw files" });
    await userEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not change file association.");
    expect(button).toBeEnabled();
    await userEvent.click(button);
    expect(associate_files).toHaveBeenCalledTimes(2);
    expect(button).toBeDisabled();
  } finally { delete window.pywebview; }
});

it("chooses the Resolve edition and audio layout from plain options", async () => {
  const base = vi.mocked(fetch).getMockImplementation()!;
  let stored = { ...saved };
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url) !== "/api/v1/settings") return base(url, init);
    if (init?.method === "PUT") stored = JSON.parse(String(init.body));
    return Response.json(stored);
  });
  render(Settings);
  const user = userEvent.setup();
  const edition = await screen.findByRole("radiogroup", { name: "Edition" });
  expect(within(edition).getByRole("radio", { name: "Free" })).toBeChecked();
  await user.click(within(edition).getByRole("radio", { name: "Studio" }));
  expect(stored.resolve_full_resolution).toBe(true);
  const audio = screen.getByRole("radiogroup", { name: "Audio" });
  expect(within(audio).getByRole("radio", { name: /With the video/ })).toBeChecked();
  await user.click(within(audio).getByRole("radio", { name: /Separate, unlinked/ }));
  expect(stored).toMatchObject({ resolve_full_resolution: true, resolve_separate_audio: true, resolve_link_audio: false });
  await user.click(within(audio).getByRole("radio", { name: /Separate, kept linked/ }));
  expect(stored).toMatchObject({ resolve_separate_audio: true, resolve_link_audio: true });
  await user.click(within(audio).getByRole("radio", { name: /With the video/ }));
  expect(stored.resolve_separate_audio).toBe(false);
});

it("mounts sent media again on start-up only when switched on", async () => {
  const base = vi.mocked(fetch).getMockImplementation()!;
  let stored = { ...saved };
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url) !== "/api/v1/settings") return base(url, init);
    if (init?.method === "PUT") stored = JSON.parse(String(init.body));
    return Response.json(stored);
  });
  render(Settings);
  const remount = await screen.findByRole("switch", { name: "Mount sent media again on start-up" });
  expect(remount).not.toBeChecked();
  await userEvent.setup().click(remount);
  expect(stored.remount_on_start).toBe(true);
});

it("adds measured keywords to Resolve sends only when switched on", async () => {
  const base = vi.mocked(fetch).getMockImplementation()!;
  let stored: Record<string, unknown> = { ...saved };
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url) !== "/api/v1/settings") return base(url, init);
    if (init?.method === "PUT") stored = JSON.parse(String(init.body));
    return Response.json(stored);
  });
  render(Settings);
  const keywords = await screen.findByRole("switch", { name: "Add measured keywords" });
  expect(keywords).not.toBeChecked();
  await userEvent.setup().click(keywords);
  expect(stored.resolve_keywords).toBe(true);
});

it("chooses time-of-day timecode and where its time zone comes from", async () => {
  const base = vi.mocked(fetch).getMockImplementation()!;
  let stored: Record<string, unknown> = { ...saved, timecode: "zero", timecode_zone: "computer" };
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url) !== "/api/v1/settings") return base(url, init);
    if (init?.method === "PUT") stored = { ...stored, ...JSON.parse(String(init.body)) };
    return Response.json(stored);
  });
  render(Settings);
  const user = userEvent.setup();
  const timecode = await screen.findByRole("radiogroup", { name: "Timecode" });
  expect(screen.queryByRole("radiogroup", { name: "Time zone" })).toBeNull();
  await user.click(within(timecode).getByRole("radio", { name: "Time of day" }));
  expect(stored.timecode).toBe("time-of-day");
  const zone = await screen.findByRole("radiogroup", { name: "Time zone" });
  await user.click(within(zone).getByRole("radio", { name: /Where it was recorded/ }));
  expect(stored.timecode_zone).toBe("location");
});
