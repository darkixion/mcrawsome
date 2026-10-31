import { render, screen, waitFor } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import ClipNotes from "../src/components/ClipNotes.svelte";
import { clearAnnotationSessions } from "../src/lib/annotations";

const values = { title: "", description: "", keywords: [], scene: "", shot: "", take: "", location: "", tag: "untagged", notes: "", camera: "" };
const record = (id: string, data = values) => ({ values: data, version: "", storage: "sidecar", message: `Saved beside the recording as ${id}.json`, document: { format: "mcrawsome.clip-details", schema_version: 1, source_id: id, details: data } });
afterEach(() => { clearAnnotationSessions(); vi.unstubAllGlobals(); });

it("offers keyboard-friendly fields and saves production logging for the active clip", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url, init) => Response.json(record(String(url).includes("/b/") ? "b" : "a", init?.body ? JSON.parse(String(init.body)).values : values))));
  render(ClipNotes, { id: "a", name: "a.mcraw" });
  const user = userEvent.setup();
  const title = await screen.findByRole("textbox", { name: "Title" });
  await waitFor(() => expect(title).toBeEnabled());
  await user.type(title, "Bentley House");
  await user.type(screen.getByRole("textbox", { name: "Description" }), "Arrival");
  await user.type(screen.getByRole("textbox", { name: "Keywords" }), "exterior, arrival");
  await user.click(screen.getByText("Production details", { exact: true }));
  for (const [label, value] of ([["Scene", "12B"], ["Shot", "C"], ["Take", "14A"], ["Location", "Bentley House Exterior"], ["Camera", "A"], ["Notes", "Clean sound"]] as const)) {
    await user.type(screen.getByRole("textbox", { name: label }), value);
  }
  await user.selectOptions(screen.getByRole("combobox", { name: "Tag" }), "rejected");
  await user.click(screen.getByRole("button", { name: "Save details now" }));
  await waitFor(() => expect(screen.getByRole("status", { name: "Clip details save status" })).toHaveTextContent("Saved"));
  const sent = vi.mocked(fetch).mock.calls.filter(call => call[1]?.method === "PUT").at(-1)!;
  expect(JSON.parse(String(sent[1]!.body)).values).toMatchObject({ title: "Bentley House", description: "Arrival", scene: "12B", take: "14A", location: "Bentley House Exterior", tag: "rejected" });
});

it("retains a draft through rapid clip changes and never shows a late reply on another clip", async () => {
  let finish: (value: Response) => void = () => {};
  vi.stubGlobal("fetch", vi.fn(async (url, init) => {
    if (init?.body && String(url).includes("/a/")) return new Promise<Response>(resolve => { finish = resolve; });
    return Response.json(record(String(url).includes("/b/") ? "b" : "a"));
  }));
  const view = render(ClipNotes, { id: "a", name: "a.mcraw" });
  const user = userEvent.setup();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Title" })).toBeEnabled());
  await user.type(screen.getByRole("textbox", { name: "Title" }), "First clip");
  await view.rerender({ id: "b", name: "b.mcraw" });
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Title" })).toHaveValue(""));
  finish(Response.json(record("a", { ...values, title: "First clip" })));
  await waitFor(() => expect(screen.getByRole("status", { name: "Clip details save status" })).toHaveTextContent("Saved"));
  expect(screen.getByRole("textbox", { name: "Title" })).toHaveValue("");
});

it("stays on the clip when Save and next cannot save its draft", async () => {
  const onnext = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => init?.body
    ? Response.json({ error: { code: "recipe-changed", message: "Changed elsewhere" } }, { status: 409 })
    : Response.json(record("a"))));
  render(ClipNotes, { id: "a", name: "a.mcraw", onnext });
  const user = userEvent.setup();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Title" })).toBeEnabled());
  await user.type(screen.getByRole("textbox", { name: "Title" }), "Keep my draft");
  await user.click(screen.getByRole("button", { name: "Save details and next clip" }));
  await waitFor(() => expect(screen.getByRole("status", { name: "Clip details save status" })).toHaveTextContent("Not saved"));
  expect(onnext).not.toHaveBeenCalled();
  expect(screen.getByRole("textbox", { name: "Title" })).toHaveValue("Keep my draft");
  await user.click(screen.getByRole("button", { name: "Discard edits and reload" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Title" })).toHaveValue(""));
  await user.click(screen.getByRole("button", { name: "Save details and next clip" }));
  expect(onnext).toHaveBeenCalledOnce();
});

it("focuses the next clip's title after Save and next completes", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url, init) => Response.json(record(String(url).includes("/b/") ? "b" : "a", init?.body ? JSON.parse(String(init.body)).values : values))));
  const view = render(ClipNotes, { id: "a", name: "a.mcraw", onnext: () => { void view.rerender({ id: "b", name: "b.mcraw", onnext: undefined }); } });
  const user = userEvent.setup();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Title" })).toBeEnabled());
  await user.click(screen.getByText("Production details"));
  await user.type(screen.getByRole("textbox", { name: "Notes" }), "First clip notes");
  await user.keyboard("{Control>}{Enter}{/Control}");
  await waitFor(() => expect(screen.getByRole("region", { name: "Clip details editor" })).toHaveTextContent("b.mcraw"));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Title" })).toHaveFocus());
  expect(screen.getByRole("textbox", { name: "Title" })).toHaveValue("");
});

it("suggests the nearest place for Location and fills it when asked", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => Response.json(record("a", init?.body ? JSON.parse(String(init.body)).values : values))));
  render(ClipNotes, { id: "a", name: "a.mcraw", place: "Bristol, United Kingdom" });
  const user = userEvent.setup();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Title" })).toBeEnabled());
  await user.click(screen.getByText("Production details", { exact: true }));
  expect(screen.getByText(/Recorded near Bristol, United Kingdom/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Use as Location" }));
  expect(screen.getByRole("textbox", { name: "Location" })).toHaveValue("Bristol, United Kingdom");
  expect(screen.queryByRole("button", { name: "Use as Location" })).toBeNull();
});

it("names a distant city with its distance and does not offer it as the Location", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => Response.json(record("a", init?.body ? JSON.parse(String(init.body)).values : values))));
  render(ClipNotes, { id: "a", name: "a.mcraw", place: "Biggin Hill, United Kingdom", placeKm: 26.7 });
  const user = userEvent.setup();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Title" })).toBeEnabled());
  await user.click(screen.getByText("Production details", { exact: true }));
  expect(screen.getByText(/Recorded about 27 km from Biggin Hill, United Kingdom/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Use as Location" })).toBeNull();
});
