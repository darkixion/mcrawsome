import { get } from "svelte/store";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { analysis, connect, engine, mediaUse, sendStage } from "../src/lib/events";
import { missingFolders } from "../src/lib/library";
import { FakeEventSource } from "./fake-event-source";


let stop: () => void = () => {};

beforeEach(() => {
  vi.useFakeTimers();
  FakeEventSource.instances = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ version: "0.1.0", active_mounts: 0 }), { status: 200 })),
  );
});

afterEach(() => {
  stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function start(): FakeEventSource {
  stop = connect((url) => new FakeEventSource(url) as unknown as EventSource);
  const source = FakeEventSource.instances.at(-1);
  if (source === undefined) throw new Error("no EventSource was opened");
  return source;
}

describe("events", () => {
  it("opens one EventSource on the event stream", () => {
    start();
    expect(FakeEventSource.instances.map((source) => source.url)).toEqual(["/api/v1/events"]);
  });

  it("sets the engine to reconnecting on an error and back to connected on the next message", async () => {
    const source = start();
    source.open();
    expect(get(engine)).toBe("connected");
    source.fail();
    expect(get(engine)).toBe("reconnecting");
    source.send("message", {});
    expect(get(engine)).toBe("connected");
  });

  it("opens a new stream with a growing delay when the browser gives up, then stopped", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("offline"));
    let source = start();
    const delays: number[] = [];
    for (let attempt = 0; attempt < 6; attempt++) {
      source.fail(true);
      expect(source.closed).toBe(true);
      const before = FakeEventSource.instances.length;
      let waited = 0;
      while (FakeEventSource.instances.length === before) {
        await vi.advanceTimersByTimeAsync(250);
        waited += 250;
      }
      delays.push(waited);
      source = FakeEventSource.instances.at(-1) as FakeEventSource;
    }
    expect(delays[1]).toBeGreaterThan(delays[0] as number);
    expect(get(engine)).toBe("stopped");
    source.open();
    expect(get(engine)).toBe("connected");
  });

});

it("checks health after five seconds without events before declaring the engine stopped", async () => {
  const source = start();
  source.open();
  vi.mocked(fetch).mockRejectedValue(new TypeError("offline"));
  source.fail();
  await vi.advanceTimersByTimeAsync(4999);
  expect(get(engine)).toBe("reconnecting");
  await vi.advanceTimersByTimeAsync(1);
  expect(fetch).toHaveBeenCalledWith("/api/v1/health", expect.anything());
  expect(get(engine)).toBe("stopped");
});

it("keeps reconnecting when events fail but health answers", async () => {
  const source = start();
  source.open();
  for (let i = 0; i < 6; i++) source.fail();
  await vi.advanceTimersByTimeAsync(5000);
  expect(get(engine)).toBe("reconnecting");
});

it.each(["open", "message", "library", "analysis", "media-use", "send", "error"])("ignores a late %s callback after stop", async (kind) => {
  const source = start();
  source.open();
  stop();
  const missing = get(missingFolders);
  const analysed = get(analysis);
  const staged = get(sendStage);
  if (kind === "open") source.open();
  else if (kind === "error") source.fail(true);
  else if (kind === "analysis") source.send(kind, { id: "a" });
  else if (kind === "send") source.send(kind, { send: "late", mount: null, stage: "Writing the bin" });
  else source.send(kind, { clips: [], removed: [], removed_paths: [], missing_folders: ["/gone"] });
  expect(get(missingFolders)).toEqual(missing);
  expect(get(analysis)).toEqual(analysed);
  expect(get(sendStage)).toEqual(staged);
  expect(get(engine)).toBe("connected");
  expect(vi.getTimerCount()).toBe(0);
});


it("receives media use changes without polling the mount listing", () => {
  const source = start();
  source.send("media-use", { abc: true });
  expect(get(mediaUse)).toEqual({ abc: true });
  source.send("media-use", { abc: false, old: null });
  expect(get(mediaUse)).toEqual({ abc: false, old: null });
  expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/mounts"))).toBe(false);
});


it("passes on the stages of a send", () => {
  const source = start();
  source.send("send", { send: "page-token", mount: "mount-a", stage: "Reading its gyro" });
  expect(get(sendStage)).toEqual({ send: "page-token", mount: "mount-a", stage: "Reading its gyro" });
  expect(get(engine)).toBe("connected");
});


it("does not fetch retired job APIs on connect or reconnect", () => {
  const source = start();
  source.open(); source.fail(); source.open();
  expect(vi.mocked(fetch).mock.calls.some(([url]) => /\/(jobs|queue)(\/|$)/.test(String(url)))).toBe(false);
});
