import { get } from "svelte/store";
import { afterEach, expect, it, vi } from "vitest";
import { AnnotationSession } from "../src/lib/annotations";

const values = { title: "", description: "", keywords: [], scene: "", shot: "", take: "", location: "", tag: "untagged", notes: "", camera: "" };
const record = (title = "", version = "") => ({ values: { ...values, title }, version, storage: "sidecar", message: "Saved beside the recording as a.json", document: { format: "mcrawsome.clip-details", schema_version: 1, source_id: "a", details: { ...values, title } } });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it("autosaves production details without changing recipe settings", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => !init?.body ? Response.json(record()) : Response.json({ ...record("Bentley House", "v1"), values: JSON.parse(String(init.body)).values })));
  const session = new AnnotationSession("a");
  await session.load();
  session.edit({ title: "Bentley House", take: "14A", tag: "good-take" });
  session.editKeywords("exterior, arrival, ");
  expect(get(session.state).dirty).toBe(true);
  await vi.advanceTimersByTimeAsync(600);
  expect(get(session.state).dirty).toBe(false);
  expect(get(session.state).keywordText).toBe("exterior, arrival, ");
  const [url, init] = vi.mocked(fetch).mock.calls[1]!;
  expect(url).toBe("/api/v1/clips/a/annotations");
  expect(JSON.parse(String(init!.body))).toMatchObject({ version: "", values: { take: "14A", keywords: ["exterior", "arrival"], tag: "good-take" } });
});

it("serialises edits arriving during a save and keeps their clip identities separate", async () => {
  let finish: (value: Response) => void = () => {};
  const calls: { id: string; values: typeof values; version: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url, init) => {
    if (!init?.body) return Response.json(record());
    const data = JSON.parse(String(init.body)); calls.push({ id: String(url), ...data });
    if (calls.length === 1) return new Promise<Response>(resolve => { finish = resolve; });
    return Response.json({ ...record(data.values.title, "v2"), values: data.values });
  }));
  const a = new AnnotationSession("a"), b = new AnnotationSession("b");
  await a.load();await b.load();
  a.edit({ title: "First" });
  const saved = a.flush();
  a.edit({ title: "Second" });b.edit({ title: "Other clip" });
  finish(Response.json(record("First", "v1")));
  await saved;await b.flush();
  expect(calls.map(call => [call.id, call.values.title, call.version])).toEqual([
    ["/api/v1/clips/a/annotations", "First", ""], ["/api/v1/clips/a/annotations", "Second", "v1"], ["/api/v1/clips/b/annotations", "Other clip", ""],
  ]);
  expect(get(a.state).values!.title).toBe("Second");
  expect(get(b.state).values!.title).toBe("Other clip");
});

it("keeps edits after a version conflict and requires an explicit reload", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => !init?.body ? Response.json(record("Saved")) : Response.json({ error: { code: "recipe-changed", message: "Changed elsewhere" } }, { status: 409 })));
  const session = new AnnotationSession("a");
  await session.load();session.edit({ title: "My draft" });
  expect(await session.flush()).toBe(false);
  expect(get(session.state).values!.title).toBe("My draft");
  expect(get(session.state).dirty).toBe(true);
  await session.load(); // ordinary navigation must not discard a failed draft
  expect(get(session.state).values!.title).toBe("My draft");
  await session.load(true);
  expect(get(session.state).values!.title).toBe("Saved");
  expect(get(session.state).dirty).toBe(false);
});

it("keeps a mounted clip's save session when the library has more than 64 clips", async () => {
  const { annotationsFor, clearAnnotationSessions } = await import("../src/lib/annotations");
  clearAnnotationSessions();
  const held = annotationsFor("held");
  const release = held.retain();
  try {
    for (let index = 0; index < 70; index++) annotationsFor(`clip-${index}`);
    expect(annotationsFor("held")).toBe(held);
  } finally { release(); clearAnnotationSessions(); }
});
