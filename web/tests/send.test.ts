import { get } from "svelte/store";
import { render, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { sendRaw, sendActivity, sendHistory, restoreSendHistory, sendChoices, sendKey, type SendTarget } from "../src/lib/send";
import { sendStage } from "../src/lib/events";
import SendActivity from "../src/components/SendActivity.svelte";

it("mounts four at a time, reports progress and sends in selection order", async () => {
  const waiting = new Map<string, (response: Response) => void>();
  vi.stubGlobal("fetch", vi.fn(async (_input, init) => {
    if (!init?.body) return Response.json({ available: true, reason: "Available", mounts: [] });
    const id = JSON.parse(String(init.body)).clip_id as string;
    return new Promise<Response>(resolve => waiting.set(id, resolve));
  }));
  const send = vi.fn(async (ids: string[]) => ({ ok: true as const, data: { launched: true, results: ids.map(id => ({ id: `mount-${id}`, added: false, waiting: false, colour_set: false, message: "Dispatched" })) } }));
  render(SendActivity);
  const running = sendRaw(Array.from({ length: 6 }, (_, i) => ({ id: String(i), name: `${i}.mcraw` })), { ...target, send }, vi.fn());
  await waitFor(() => expect(waiting.size).toBe(4));
  expect(screen.getByRole("progressbar", { name: "Mount preparation" })).toHaveAttribute("value", "0");
  waiting.get("2")!(Response.json({ id: "m2" }));
  await waitFor(() => expect(waiting.has("4")).toBe(true));
  expect(screen.getByRole("progressbar", { name: "Mount preparation" })).toHaveAttribute("value", "1");
  waiting.get("0")!(Response.json({ id: "m0" }));
  await waitFor(() => expect(waiting.has("5")).toBe(true));
  for (const id of ["5", "4", "3", "1"]) waiting.get(id)!(Response.json({ id: `m${id}` }));
  await running;
  expect(send).toHaveBeenCalledExactlyOnceWith(["m0", "m1", "m2", "m3", "m4", "m5"], expect.any(String));
  expect(screen.queryByRole("progressbar")).toBeNull();
});

it("accepts a forty-clip selection", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => !init?.body
    ? Response.json({ available: true, reason: "Available", mounts: [] })
    : Response.json({ id: JSON.parse(String(init.body)).clip_id })));
  const send = vi.fn(async (_ids: string[]) => ({ ok: true as const, data: { launched: true, results: [] } }));
  await sendRaw(Array.from({ length: 40 }, (_, i) => ({ id: String(i), name: `${i}.mcraw` })), { ...target, send }, vi.fn());
  expect(send).toHaveBeenCalledOnce();
  expect(send.mock.calls[0]?.[0]).toHaveLength(40);
});

beforeEach(() => { sendHistory.set([]); sendActivity.set(null); localStorage.clear(); });
afterEach(() => vi.unstubAllGlobals());
const target: SendTarget = { id: "test-target", label: "Another editor", disabled: () => null,
  send: vi.fn(async () => ({ ok: true as const, data: { launched: true, archive: "/state/example.bundle", results: [{ id: "mount-m-a", added: false, colour_set: false, waiting: false, message: "Opened archive" }] } })),
};

it("refreshes virtual media after dispatch creates recorded-orientation dependencies", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => !init?.body
    ? Response.json({ available: true, reason: "Available", mounts: [] }) : Response.json({ id: "selected-90" })));
  const refresh = vi.fn();
  const send = vi.fn(async () => {
    expect(refresh).toHaveBeenCalledTimes(1);
    return { ok: true as const, data: { launched: true, results: [{ id: "mount-selected-90", message: "Sent", added: false, colour_set: false, waiting: false }] } };
  });
  await sendRaw([{ id: "a", name: "a.mcraw" }], { ...target, send }, refresh);
  expect(refresh).toHaveBeenCalledTimes(2);
});

it("continues past a failed clip and sends successful mounts to the requested target", async () => {
  vi.mocked(target.send).mockClear();
  vi.stubGlobal("fetch", vi.fn(async (_input, init) => {
    if (!init?.body) return Response.json({ available: true, reason: "Available", mounts: [] });
    const id = JSON.parse(String(init.body)).clip_id;
    if (id === "b") return Response.json({ error: { code: "input-unreadable", message: "Source offline" } }, { status: 422 });
    return Response.json({ id: "m-a" }, { status: 201 });
  }));
  await sendRaw([{ id: "b", name: "b.mcraw" }, { id: "a", name: "a.mcraw" }], target, vi.fn());
  expect(target.send).toHaveBeenCalledExactlyOnceWith(["m-a"], expect.any(String));
  expect(get(sendHistory).map(item => [item.id, item.state, item.target])).toEqual([["b", "failed", "test-target"], ["a", "sent", "test-target"]]);
  expect(get(sendActivity)?.busy).toBe(false);
  sendHistory.set([]);
  restoreSendHistory();
  expect(get(sendHistory)).toHaveLength(2);
  expect(get(sendHistory).find(item => item.id === "a")?.archive).toBe("/state/example.bundle");
});

it("does not create mounts when the destination is disabled", async () => {
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  await sendRaw([{ id: "a", name: "a.mcraw" }], { ...target, disabled: () => "Sending is off" }, vi.fn());
  expect(fetch).not.toHaveBeenCalled();
  expect(get(sendHistory)[0]?.state).toBe("failed");
  expect(get(sendHistory)[0]?.message).toBe("Sending is off");
});

it("guards a pending send against duplicate clicks and records unconfirmed activation as a failure", async () => {
  let finish: (response: Response) => void = () => {};
  const fetch = vi.fn(() => new Promise<Response>(resolve => { finish = resolve; }));
  vi.stubGlobal("fetch", fetch);
  const first = sendRaw([{ id: "a", name: "a.mcraw" }], target, vi.fn());
  await sendRaw([{ id: "a", name: "a.mcraw" }], target, vi.fn());
  expect(fetch).toHaveBeenCalledTimes(1);
  finish(Response.json({ available: false, reason: "FUSE unavailable", mounts: [] }));
  await first;
  expect(get(sendActivity)?.busy).toBe(false);
  expect(get(sendHistory)[0]?.state).toBe("failed");
});

it("ignores malformed persisted history", () => {
  for (const raw of ["invalid-json", "{}", '[{"id":"a","state":"sent"}]']) {
    localStorage.setItem("mcrawsome.raw-sends.v1", raw);
    restoreSendHistory();
    expect(get(sendHistory)).toEqual([]);
  }
});

it("sends the chosen Resolve mode explicitly without creating a second destination", async () => {
  const fetch = vi.fn(async (_input: unknown, _init?: RequestInit) => Response.json({ launched: true, results: [] }));
  vi.stubGlobal("fetch", fetch);
  const timeline = sendChoices.find(target => target.mode === "timeline")!;
  expect(timeline.id).toBe("resolve");
  await timeline.send(["m-a"]);
  expect(JSON.parse(fetch.mock.calls[0]![1]!.body as string)).toEqual({ mounts: ["m-a"], mode: "timeline", gyro: false, unchecked_gyro: false, rolling_shutter: false });
});

it("keeps both clips sent when a paired timeline was dispatched but import is not confirmed", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => !init?.body
    ? Response.json({ available: true, reason: "Available", mounts: [] })
    : Response.json({ id: `m-${JSON.parse(String(init.body)).clip_id}` }, { status: 201 })));
  const timeline: SendTarget = { id: "resolve", label: "Resolve (Timeline)", disabled: () => null,
    send: vi.fn(async () => ({ ok: true as const, data: { launched: true, archive: "/state/paired.drt", results: ["a", "b"].map(id => ({ id: `mount-m-${id}`, added: false, colour_set: false, waiting: false, message: "Sent to desktop file handler" })) } })),
  };
  await sendRaw([{ id: "a", name: "a.mcraw" }, { id: "b", name: "b.mcraw" }], timeline, vi.fn());
  expect(get(sendHistory).map(item => item.state)).toEqual(["sent", "sent"]);
  sendActivity.set(null);sendHistory.set([]);restoreSendHistory();
  expect(get(sendHistory).map(item => item.state)).toEqual(["sent", "sent"]);
});

it("offers Media Bin and Timeline only: a timeline stabilises by each clip's Stabilise switch", async () => {
  expect(sendChoices.map(target => target.label)).toEqual(["Resolve (Media Bin)", "Resolve (Timeline)"]);
  const fetch = vi.fn(async (_input: unknown, _init?: RequestInit) => Response.json({ launched: true, results: [] }));
  vi.stubGlobal("fetch", fetch);
  await sendChoices[1]!.send(["m-a"]);
  expect(JSON.parse(fetch.mock.calls[0]![1]!.body as string)).toEqual({ mounts: ["m-a"], mode: "timeline", gyro: false, unchecked_gyro: false, rolling_shutter: false });
  expect(sendKey(sendChoices[1]!)).toBe("resolve:timeline");
});

it("expands a failed send to show the underlying error", () => {
  sendActivity.set({ target: "resolve", targetLabel: "Resolve (Timeline + Gyro)", busy: false, entries: [{
    id: "a", name: "clip.mcraw", target: "resolve", targetLabel: "Resolve (Timeline + Gyro)", state: "failed",
    message: "The gyro log cannot be read", archive: null, at: new Date().toISOString(),
  }] });
  render(SendActivity);
  expect(screen.getByText(/The gyro log cannot be read/).closest("details")).toHaveAttribute("open");
});

it("shows the stage the engine reports for this send until the send ends", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => !init?.body
    ? Response.json({ available: true, reason: "Available", mounts: [] }) : Response.json({ id: "m-a" }, { status: 201 })));
  let finish: () => void = () => {};
  const send = vi.fn(async (_ids: string[], progress?: string) => {
    sendStage.set({ send: "another-page", mount: null, stage: "Writing the bin" });
    sendStage.set({ send: progress!, mount: "mount-m-a", stage: "Tracking image detail" });
    await new Promise<void>(resolve => { finish = resolve; });
    return { ok: true as const, data: { launched: true, results: [{ id: "mount-m-a", message: "Opened", added: false, colour_set: false, waiting: false }] } };
  });
  render(SendActivity);
  const running = sendRaw([{ id: "a", name: "a.mcraw" }], { ...target, send }, vi.fn());
  const activity = await screen.findByRole("status", { name: "Send activity" });
  await waitFor(() => expect(activity).toHaveTextContent("Sending 1 clip to Another editor · Tracking image detail"));
  expect(screen.getByText("a.mcraw").parentElement).toHaveTextContent("a.mcraw · Tracking image detail");
  const token = send.mock.calls[0]![1]!;
  expect(token).toMatch(/^[A-Za-z0-9-]{1,64}$/);
  sendStage.set({ send: token, mount: null, stage: "Opening it in Resolve" });
  await waitFor(() => expect(activity).toHaveTextContent("Sending 1 clip to Another editor · Opening it in Resolve"));
  finish();
  await running;
  sendStage.set({ send: token, mount: "mount-m-a", stage: "Writing the timeline" });
  expect(get(sendActivity)?.entries[0]?.message).toBe("Opened");
  expect(activity).toHaveTextContent("1 clip sent to Another editor");
});

it("names the clip a stage belongs to when the send has several", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => !init?.body
    ? Response.json({ available: true, reason: "Available", mounts: [] })
    : Response.json({ id: `m-${JSON.parse(String(init.body)).clip_id}` }, { status: 201 })));
  const send = vi.fn(async (_ids: string[], progress?: string) => {
    sendStage.set({ send: progress!, mount: "mount-m-b", stage: "Reading its gyro" });
    expect(get(sendActivity)?.stage).toBe("b.mcraw: Reading its gyro");
    expect(get(sendActivity)?.entries.map(entry => entry.message)).toEqual(["Creating handoff and sending to the receiving application", "Reading its gyro"]);
    sendStage.set({ send: progress!, mount: null, stage: "Writing the timeline" });
    expect(get(sendActivity)?.stage).toBe("Writing the timeline");
    return { ok: true as const, data: { launched: true, results: [] } };
  });
  await sendRaw([{ id: "a", name: "a.mcraw" }, { id: "b", name: "b.mcraw" }], { ...target, send }, vi.fn());
  expect(send).toHaveBeenCalledOnce();
});

it("restores the saved history", () => {
  const entry = { id: "a", name: "a.mcraw", target: "resolve", targetLabel: "Resolve", message: "Sent", at: "2026-10-29T12:00:00Z", state: "sent", archive: null };
  localStorage.setItem("mcrawsome.raw-sends.v1", JSON.stringify([entry]));
  sendHistory.set([]);
  restoreSendHistory();
  expect(get(sendHistory).map(item => item.id)).toEqual(["a"]);
  localStorage.removeItem("mcrawsome.raw-sends.v1");
});

describe("a finished send", () => {
  function engine(): void {
    vi.stubGlobal("fetch", vi.fn(async (_url, init) => !init?.body
      ? Response.json({ available: true, reason: "Available", mounts: [] }) : Response.json({ id: `m-${JSON.parse(String(init.body)).clip_id}` })));
  }
  const resolve: SendTarget = { ...sendChoices[1]!, send: vi.fn(async (ids: string[]) => ({ ok: true as const, data: { launched: true, results: ids.map(id => ({ id: `mount-${id}`, added: false, waiting: false, message: "Opened" })) } })) };
  const clips = [{ id: "a", name: "a.mcraw" }, { id: "b", name: "b.mcraw" }];
  afterEach(() => { vi.restoreAllMocks(); delete (window as { pywebview?: unknown }).pywebview; });

  it("tells the desktop when the window is not in front", async () => {
    const notify = vi.fn(async () => true);
    (window as { pywebview?: unknown }).pywebview = { api: { notify } };
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    engine();
    await sendRaw(clips, resolve, vi.fn());
    expect(notify).toHaveBeenCalledExactlyOnceWith("2 clips sent to Resolve (Timeline)", "Complete the import in Resolve.");
  });

  it("names the clip that failed", async () => {
    const notify = vi.fn(async () => true);
    (window as { pywebview?: unknown }).pywebview = { api: { notify } };
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    vi.stubGlobal("fetch", vi.fn(async (_url, init) => {
      if (!init?.body) return Response.json({ available: true, reason: "Available", mounts: [] });
      const id = JSON.parse(String(init.body)).clip_id;
      return id === "b" ? Response.json({ error: { code: "clip-missing", message: "No longer in the library folders" } }, { status: 404 })
        : Response.json({ id: `m-${id}` });
    }));
    await sendRaw(clips, resolve, vi.fn());
    expect(notify).toHaveBeenCalledExactlyOnceWith("1 of 2 clips sent to Resolve (Timeline)", "b.mcraw: No longer in the library folders");
  });

  it("keeps quiet while the window is in front", async () => {
    const notify = vi.fn(async () => true);
    (window as { pywebview?: unknown }).pywebview = { api: { notify } };
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    engine();
    await sendRaw(clips, resolve, vi.fn());
    expect(notify).not.toHaveBeenCalled();
  });

  it("uses a browser's notifications once they are allowed, never asking", async () => {
    const shown: [string, NotificationOptions | undefined][] = [];
    class Shown { static permission = "default"; constructor(title: string, options?: NotificationOptions) { shown.push([title, options]); } }
    vi.stubGlobal("Notification", Shown);
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    engine();
    await sendRaw(clips, resolve, vi.fn());
    expect(shown).toEqual([]);
    Shown.permission = "granted";
    await sendRaw(clips, resolve, vi.fn());
    expect(shown).toEqual([["2 clips sent to Resolve (Timeline)", { body: "Complete the import in Resolve." }]]);
  });
});
