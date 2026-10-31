import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import SessionBar from "../src/components/SessionBar.svelte";
const workspace = { paths: ["/clips/a.mcraw"], selection: ["a"], focused: "a", search: "", kind: "all" as const, sort: "name" as const, facets: {} };
const empty = { name: "Untitled", path: null, portable: false, workspace: { ...workspace, paths: [], selection: [], focused: null }, clips: [], missing: [], recent: [], problem: null };
afterEach(() => { vi.unstubAllGlobals(); });
it("restores the saved workspace and saves portable sessions to a chosen path", async () => {
  const restored = { ...empty, workspace, recent: ["/clips/edit.mcrawsome-session.json"] };
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => Response.json(init?.method === "POST" ? { ...restored, path: "/clips/edit.mcrawsome-session.json", portable: true } : restored)));
  const onrestore = vi.fn();
  const view = render(SessionBar, { workspace, onrestore });
  await waitFor(() => expect(onrestore).toHaveBeenCalledWith(restored));
  await fireEvent.click(view.container.querySelector("summary")!);
  await fireEvent.input(screen.getByRole("textbox", { name: "Session file path" }), { target: { value: "/clips/edit.mcrawsome-session.json" } });
  await fireEvent.click(screen.getByRole("button", { name: "Save with relative paths" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/v1/session/save", expect.objectContaining({ body: JSON.stringify({ path: "/clips/edit.mcrawsome-session.json", portable: true }) })));
  await fireEvent.click(view.container.querySelector("summary")!);
  expect(screen.getByRole("combobox", { name: "Recent sessions" })).toHaveTextContent("edit.mcrawsome-session.json");
});
it("auto-saves changes and reports a failed load without replacing the workspace", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url) => String(url).endsWith("/load") ? Response.json({ error: { code: "input-unreadable", message: "Invalid session" } }, { status: 422 }) : Response.json(empty)));
  const onrestore = vi.fn();
  const view = render(SessionBar, { workspace, onrestore });
  await screen.findByText("Untitled");
  await view.rerender({ workspace: { ...workspace, search: "arrival" } });
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/v1/session", expect.objectContaining({ method: "PUT", body: expect.stringContaining('"search":"arrival"') })));
  await fireEvent.click(view.container.querySelector("summary")!);
  await fireEvent.input(screen.getByRole("textbox", { name: "Session file path" }), { target: { value: "/bad.mcrawsome-session.json" } });
  await fireEvent.click(screen.getByRole("button", { name: "Load" }));
  await screen.findByText("Invalid session");
  expect(onrestore).not.toHaveBeenCalled();
});

it("flushes pending clip changes and captures a session even if workspace layout is unchanged", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url, init) => { if (init?.method) calls.push(`${init.method} ${url}`); return Response.json({ ...empty, workspace }); }));
  const beforechange = vi.fn(async () => { calls.push("flush clip"); return true; });
  const view = render(SessionBar, { workspace, onrestore: vi.fn(), beforechange });
  await screen.findByText("Untitled");
  await fireEvent.click(view.container.querySelector("summary")!);
  await fireEvent.input(screen.getByRole("textbox", { name: "Session file path" }), { target: { value: "/clips/edit.mcrawsome-session.json" } });
  await fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(calls).toContain("POST /api/v1/session/save"));
  expect(calls.slice(-3)).toEqual(["flush clip", "PUT /api/v1/session", "POST /api/v1/session/save"]);
});
it("starts a new session holding the chosen shoot's clips", async () => {
  const shootPaths = ["/clips/a.mcraw", "/clips/b.mcraw"];
  const filled = { ...empty, workspace: { ...empty.workspace, paths: shootPaths } };
  let opened = false;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/v1/opened-clips" && init?.method === "POST") { opened = true; return Response.json({ clips: [], missing_folders: [] }); }
    return Response.json(opened && url === "/api/v1/session" && init?.method === "GET" ? filled : empty);
  }));
  const onrestore = vi.fn();
  const view = render(SessionBar, { workspace, onrestore, shoot: { label: "25 Oct 2026, 13:44", paths: shootPaths } });
  await screen.findByText("Untitled");
  await fireEvent.click(view.container.querySelector("summary")!);
  await fireEvent.click(screen.getByRole("button", { name: "New session from this shoot (2 clips)" }));
  await waitFor(() => expect(onrestore).toHaveBeenLastCalledWith(filled));
  expect(fetch).toHaveBeenCalledWith("/api/v1/opened-clips", expect.objectContaining({ method: "POST", body: JSON.stringify({ paths: shootPaths }) }));
});
