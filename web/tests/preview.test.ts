import { get } from "svelte/store";
import { afterEach, expect, it, vi } from "vitest";
import { PreviewClient } from "../src/lib/preview";

class Socket {
  binaryType = "";
  onmessage: ((event: { data: string | ArrayBuffer }) => void) | null = null;
  onclose: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn();
  reply(seq: number, kind = "draft") {
    this.onmessage?.({ data: JSON.stringify({ seq, kind, bytes: 1, ms: 12 }) });
    this.onmessage?.({ data: new ArrayBuffer(1) });
  }
}
afterEach(() => vi.unstubAllGlobals());
it("clears unused browser images while keeping the displayed picture and accurate estimates", () => {
  let serial = 0;
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => `blob:${++serial}`), revokeObjectURL: vi.fn() });
  vi.stubGlobal("Image", class { src = ""; });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  for (let index = 0; index < 3; index++) {
    client.request("frame", { index });
    socket.onmessage?.({ data: JSON.stringify({ seq: index + 1, index, kind: "draft", bytes: 1, width: 2, height: 2 }) });
    socket.onmessage?.({ data: new ArrayBuffer(1) });
  }
  expect(client.cacheUsage()).toEqual({ estimated_bytes: 51, limit_bytes: 64 * 1024 ** 2, entries: 3, reclaimable_entries: 2 });
  client.clearFrameCache();
  expect(client.cacheUsage()).toMatchObject({ estimated_bytes: 17, entries: 1, reclaimable_entries: 0 });
  expect(get(client.state).frame).toBe("blob:3");
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:1");
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:2");
  expect(URL.revokeObjectURL).not.toHaveBeenCalledWith("blob:3");
  client.close();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:3");
});

it("keeps pending playback decodes alive when unused browser cache entries are cleared", async () => {
  const loads: (() => void)[] = [];
  let serial = 0;
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => `blob:${++serial}`), revokeObjectURL: vi.fn() });
  vi.stubGlobal("Image", class { src = ""; decode() { return new Promise<void>(resolve => loads.push(resolve)); } });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  try {
    client.request("frame", { index: 0, playback: true, ahead: 3 });
    socket.onmessage?.({ data: JSON.stringify({ seq: 1, index: 2, kind: "draft", bytes: 2, playback: true, buffer_end: true }) });
    socket.onmessage?.({ data: new ArrayBuffer(2) });
    client.clearFrameCache();
    expect(client.buffered).toBe(1);
    expect(client.cacheUsage().reclaimable_entries).toBe(0);
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    loads.forEach(resolve => resolve());
    await Promise.resolve(); await Promise.resolve();
    client.present(2);
    expect(get(client.state)).toMatchObject({ frame: "blob:1", header: { index: 2 } });
  } finally { client.close(); }
});

it("prepares from the next Play position only for settled paused requests", () => {
  for (const [args, expected] of [[{ index: 3 }, 25], [{ index: 3, playback: true }, undefined], [{ index: 3, scrub: true }, undefined]] as const) {
    const socket = new Socket();
    const client = new PreviewClient("a", () => socket as unknown as WebSocket);
    client.warmFrom = () => 25;
    client.request("frame", args);
    expect(JSON.parse(socket.send.mock.calls[0]![0]).warm_from).toBe(expected);
    client.close();
  }
});
it("shows only the newest slider reply and revokes replaced and closed URLs", () => {
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:frame"), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  for (const exposure of [1, 2, 3]) client.request("recipe", { recipe: { develop: { exposure } } });
  socket.reply(2);
  expect(get(client.state).frame).toBeNull();
  socket.reply(3);
  expect(get(client.state).ms).toBe(12);
  socket.reply(1);
  expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
  socket.reply(3, "refined");
  expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1);
  client.close();
  expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2);
});

it("shows playback frames before requesting the latest waiting position", () => {
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:frame"), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  client.request("frame", { index: 1, playback: true });
  client.request("frame", { index: 2, playback: true });
  client.request("frame", { index: 3, playback: true });
  expect(socket.send).toHaveBeenCalledTimes(1);
  socket.reply(1);
  expect(get(client.state).header?.seq).toBe(1);
  expect(socket.send).toHaveBeenCalledTimes(2);
  expect(JSON.parse(socket.send.mock.calls[1]![0])).toMatchObject({ index: 3, playback: true });
  const next = JSON.parse(socket.send.mock.calls[1]![0]).seq as number;
  socket.reply(next);
  expect(get(client.state).header?.seq).toBe(next);
  expect(URL.createObjectURL).toHaveBeenCalledTimes(2);
  client.close();
});

it("reconnects a dropped preview and restores its latest recipe", async () => {
  vi.useFakeTimers();
  const sockets: Socket[] = [];
  const client = new PreviewClient("a", () => { const socket = new Socket(); sockets.push(socket); return socket as unknown as WebSocket; });
  try {
    client.request("recipe", { index: 7, recipe: { look: "gritty" } });
    sockets[0]!.onclose?.();
    client.request("recipe", { index: 9, recipe: { look: "neutral", develop: { exposure: 2 } } });
    await vi.advanceTimersByTimeAsync(250);
    expect(sockets).toHaveLength(2);
    sockets[1]!.onmessage?.({ data: JSON.stringify({ kind: "opened", bytes: 0 }) });
    expect(JSON.parse(sockets[1]!.send.mock.calls[0]![0])).toMatchObject({ op: "recipe", index: 9, recipe: { look: "neutral", develop: { exposure: 2 } } });
    const seq = JSON.parse(sockets[1]!.send.mock.calls[0]![0]).seq as number;
    sockets[1]!.reply(seq);
    expect(get(client.state).problem).toBeNull();
    expect(get(client.state).frame).not.toBeNull();
  } finally { client.close(); vi.useRealTimers(); }
});

it("cancels a reconnect when the page closes", async () => {
  vi.useFakeTimers();
  const socket = new Socket();
  const create = vi.fn(() => socket as unknown as WebSocket);
  const client = new PreviewClient("a", create);
  socket.onclose?.();
  client.close();
  await vi.advanceTimersByTimeAsync(10_000);
  expect(create).toHaveBeenCalledTimes(1);
  vi.useRealTimers();
});

it("lets a manual seek replace queued playback", () => {
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  client.request("frame", { index: 1, playback: true });
  client.request("frame", { index: 2, playback: true });
  client.request("frame", { index: 50 });
  socket.reply(1);
  expect(get(client.state).frame).toBeNull();
  expect(socket.send).toHaveBeenCalledTimes(2);
  socket.reply(3);
  expect(get(client.state).frame).not.toBeNull();
  client.close();
});

it("does not reconnect a preview replaced by another window", async () => {
  vi.useFakeTimers();
  const socket = new Socket();
  const create = vi.fn(() => socket as unknown as WebSocket);
  const client = new PreviewClient("a", create);
  socket.onmessage?.({ data: JSON.stringify({ kind: "closed", bytes: 0 }) });
  socket.onclose?.();
  await vi.advanceTimersByTimeAsync(10_000);
  expect(create).toHaveBeenCalledTimes(1);
  expect(get(client.state).problem?.code).toBe("preview-replaced");
  client.close();
  vi.useRealTimers();
});

it("buffers only three future frames and presents the audio-clock position, dropping late ones", () => {
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:frame"), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  client.request("frame", { index: 0, playback: true, ahead: 3 });
  function frame(index: number, end = false) {
    socket.onmessage?.({ data: JSON.stringify({ seq: 1, kind: "draft", bytes: 2, playback: true, index, buffer_end: end }) });
    socket.onmessage?.({ data: new ArrayBuffer(2) });
  }
  frame(0); frame(1); frame(2, true);
  expect(get(client.state).header?.index).toBe(0);
  client.present(2);
  expect(get(client.state).header?.index).toBe(2);
  frame(1, true);
  expect(get(client.state).header?.index).toBe(2);
  for (let i = 3; i < 12; i++) frame(i);
  expect(client.buffered).toBeLessThanOrEqual(3);
  client.present(11);
  expect(get(client.state).header?.index).toBe(11);
  client.close();
});

it("flushes prefetched frames on a manual seek and never requests beyond the clip", () => {
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:frame"), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  socket.onmessage?.({ data: JSON.stringify({ kind: "opened", frame_count: 3 }) });
  client.request("frame", { index: 0, playback: true, ahead: 3 });
  for (const index of [0, 1, 2]) {
    socket.onmessage?.({ data: JSON.stringify({ seq: 1, kind: "draft", bytes: 2, index, playback: true, buffer_end: index === 2 }) });
    socket.onmessage?.({ data: new ArrayBuffer(2) });
  }
  expect(client.buffered).toBe(2);
  client.request("frame", { index: 2, playback: true, ahead: 3 });
  expect(socket.send).toHaveBeenCalledTimes(1);
  expect(get(client.state).header?.index).toBe(2);
  client.request("frame", { index: 0 });
  expect(client.buffered).toBe(0);
  socket.reply(3);
  client.present(2);
  expect(get(client.state).header?.seq).toBe(3);
  client.close();
});

it("aims ahead of the audio clock after a slow draft, so late work cannot starve the display", () => {
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  socket.onmessage?.({ data: JSON.stringify({ kind: "opened", rate: 24 }) });
  client.request("frame", { index: 0, playback: true, ahead: 3 });
  client.request("frame", { index: 24, playback: true, ahead: 3 });
  socket.onmessage?.({ data: JSON.stringify({ seq: 1, kind: "draft", index: 0, bytes: 1, ms: 100, playback: true, buffer_end: true }) });
  socket.onmessage?.({ data: new ArrayBuffer(1) });
  expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ index: 27 });
  client.close();
});

it("coalesces drag positions, displays completed work and requests the exact full draft on rest", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:frame"), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  try {
    client.scrub(1, { recipe: { look: "neutral" } });
    client.scrub(2, { recipe: { look: "neutral" } });
    client.scrub(9, { recipe: { look: "neutral" } });
    expect(socket.send).toHaveBeenCalledTimes(1);
    socket.reply(1);
    expect(get(client.state).frame).not.toBeNull();
    expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ index: 9, scrub: true, fast: true });
    await vi.advanceTimersByTimeAsync(150);
    expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ index: 9, scrub: false, fast: true });
    expect(socket.send).toHaveBeenCalledTimes(3);
    socket.reply(2);
    expect(get(client.state).header?.seq).toBe(1);
    const exact = JSON.parse(socket.send.mock.calls.at(-1)![0]).seq as number;
    socket.reply(exact);
    expect(get(client.state).header?.seq).toBe(exact);
  } finally { client.close(); vi.useRealTimers(); }
});

it("immediately reuses the nearest delivered frame, with earlier ties and settings invalidation", () => {
  let url = 0;
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => `blob:${++url}`), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  function reply(seq: number, index: number) {
    socket.onmessage?.({ data: JSON.stringify({ seq, index, kind: "draft", bytes: 1, width: 640, height: 360 }) });
    socket.onmessage?.({ data: new ArrayBuffer(1) });
  }
  try {
    client.request("recipe", { index: 2, recipe: { look: "neutral" } }); reply(1, 2);
    const first = get(client.state).frame;
    client.request("frame", { index: 6 }); reply(2, 6);
    client.scrub(4, { recipe: { look: "neutral" } });
    expect(get(client.state).frame).toBe(first);
    expect(get(client.state).header?.index).toBe(2);
    const before = vi.mocked(URL.createObjectURL).mock.calls.length;
    client.scrub(2, { recipe: { look: "gritty" } });
    expect(URL.createObjectURL).toHaveBeenCalledTimes(before);
    expect(get(client.state).header?.index).toBe(2);
  } finally { client.close(); }
});

it("drops cached automatic-exposure images when the measured exposure arrives", () => {
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:frame"), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  function frame(seq: number, index: number, stops: number | null) {
    socket.onmessage?.({ data: JSON.stringify({ seq, index, kind: "draft", bytes: 1, exposure: { auto: true, stops } }) });
    socket.onmessage?.({ data: new ArrayBuffer(1) });
  }
  try {
    client.request("recipe", { index: 1, recipe: { develop: { exposure: "auto" } } }); frame(1, 1, null);
    client.request("frame", { index: 5 }); frame(2, 5, 1);
    client.scrub(1, { recipe: { develop: { exposure: "auto" } } });
    expect(get(client.state).header?.index).toBe(5);
  } finally { client.close(); }
});

it("keeps the closest cached image when an older drag position finishes", () => {
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:frame"), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  function frame(seq: number, index: number) {
    socket.onmessage?.({ data: JSON.stringify({ seq, index, kind: "draft", bytes: 1 }) });
    socket.onmessage?.({ data: new ArrayBuffer(1) });
  }
  try {
    client.request("recipe", { index: 10, recipe: { look: "neutral" } }); frame(1, 10);
    client.scrub(2, { recipe: { look: "neutral" } });
    client.scrub(11, { recipe: { look: "neutral" } });
    frame(2, 2);
    expect(get(client.state).header?.index).toBe(10);
    expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ index: 11 });
  } finally { client.close(); }
});

it("bounds decoded image retention and releases every URL on close", () => {
  let count = 0;
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => `blob:${++count}`), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  for (let index = 0; index < 40; index++) {
    client.request("frame", { index });
    socket.onmessage?.({ data: JSON.stringify({ seq: index + 1, index, kind: "draft", bytes: 2, width: 3840, height: 2160 }) });
    socket.onmessage?.({ data: new ArrayBuffer(2) });
  }
  // A decoded 4K image uses about 32 MiB, so the 64 MiB bound evicts before the 24-frame limit.
  expect(vi.mocked(URL.revokeObjectURL).mock.calls.length).toBeGreaterThan(40 - 24);
  client.close();
  const revoked = vi.mocked(URL.revokeObjectURL).mock.calls.map(([url]) => url);
  expect(new Set(revoked).size).toBe(40);
  expect(revoked).toHaveLength(40);
});

it("keeps the displayed image until its replacement has decoded", async () => {
  let next = 0;
  const loads: (() => void)[] = [];
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => `blob:${++next}`), revokeObjectURL: vi.fn() });
  vi.stubGlobal("Image", class {
    src = "";
    decode() { return new Promise<void>((resolve) => { loads.push(resolve); }); }
  });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  function frame(seq: number, index: number) {
    socket.onmessage?.({ data: JSON.stringify({ seq, index, kind: "draft", bytes: 2 }) });
    socket.onmessage?.({ data: new ArrayBuffer(2) });
  }
  try {
    client.request("frame", { index: 0 }); frame(1, 0);
    loads[0]!(); await Promise.resolve(); await Promise.resolve();
    const first = get(client.state);
    client.request("frame", { index: 1 }); frame(2, 1);
    expect(get(client.state)).toBe(first);
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    loads[1]!(); await Promise.resolve(); await Promise.resolve();
    expect(get(client.state)).toMatchObject({ frame: "blob:2", header: { index: 1 } });
  } finally { client.close(); }
});

it("does not flash an obsolete image when decoding finishes after a newer manual seek", async () => {
  const loads: (() => void)[] = [];
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:frame"), revokeObjectURL: vi.fn() });
  vi.stubGlobal("Image", class {
    src = "";
    decode() { return new Promise<void>((resolve) => { loads.push(resolve); }); }
  });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  try {
    client.request("frame", { index: 0 }); socket.reply(1);
    client.request("frame", { index: 1 }); socket.reply(2);
    loads[0]!(); await Promise.resolve(); await Promise.resolve();
    expect(get(client.state).frame).toBeNull();
    loads[1]!(); await Promise.resolve(); await Promise.resolve();
    expect(get(client.state).header?.seq).toBe(2);
  } finally { client.close(); }
});

it("does not move playback backwards when image decoding finishes out of order", async () => {
  const loads: (() => void)[] = [];
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:frame"), revokeObjectURL: vi.fn() });
  vi.stubGlobal("Image", class {
    src = "";
    decode() { return new Promise<void>((resolve) => { loads.push(resolve); }); }
  });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  try {
    client.request("frame", { index: 0, playback: true, ahead: 3 });
    for (const index of [0, 1]) {
      socket.onmessage?.({ data: JSON.stringify({ seq: 1, index, kind: "draft", bytes: 1, playback: true, buffer_end: false }) });
      socket.onmessage?.({ data: new ArrayBuffer(1) });
    }
    client.present(1);
    loads[1]!(); await Promise.resolve(); await Promise.resolve();
    expect(get(client.state).header?.index).toBe(1);
    loads[0]!(); await Promise.resolve(); await Promise.resolve();
    expect(get(client.state).header?.index).toBe(1);
  } finally { client.close(); }
});

it.each([false, true])("finishes decoding a due playback frame after requesting ahead, manual seek %s", async (seek) => {
  const loads: (() => void)[] = [];
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:frame"), revokeObjectURL: vi.fn() });
  vi.stubGlobal("Image", class {
    src = "";
    decode() { return new Promise<void>((resolve) => { loads.push(resolve); }); }
  });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  try {
    client.present(10);
    client.request("frame", { index: 10, playback: true, ahead: 3 });
    client.request("frame", { index: 11, playback: true, ahead: 3 });
    socket.onmessage?.({ data: JSON.stringify({ seq: 1, index: 10, kind: "draft", bytes: 1, playback: true, buffer_end: true }) });
    socket.onmessage?.({ data: new ArrayBuffer(1) });
    expect(socket.send).toHaveBeenCalledTimes(2);
    if (seek) client.request("frame", { index: 50 });
    loads[0]!(); await Promise.resolve(); await Promise.resolve();
    if (seek) expect(get(client.state).frame).toBeNull();
    else expect(get(client.state).header?.index).toBe(10);
  } finally { client.close(); }
});

it("can show a completed drag decode while the newer position is still in flight", async () => {
  const loads: (() => void)[] = [];
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:frame"), revokeObjectURL: vi.fn() });
  vi.stubGlobal("Image", class {
    src = "";
    decode() { return new Promise<void>((resolve) => { loads.push(resolve); }); }
  });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  function frame(seq: number, index: number) {
    socket.onmessage?.({ data: JSON.stringify({ seq, index, kind: "draft", bytes: 1 }) });
    socket.onmessage?.({ data: new ArrayBuffer(1) });
  }
  try {
    client.request("recipe", { index: 0, recipe: { look: "neutral" } }); frame(1, 0);
    loads[0]!(); await Promise.resolve(); await Promise.resolve();
    client.scrub(10, { recipe: { look: "neutral" } });
    client.scrub(11, { recipe: { look: "neutral" } });
    frame(2, 10);
    expect(get(client.state).header?.index).toBe(0);
    loads[1]!(); await Promise.resolve(); await Promise.resolve();
    expect(get(client.state).header?.index).toBe(10);
    expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ index: 11 });
  } finally { client.close(); }
});

it("releases pending images on close and ignores their later decode completion", async () => {
  let next = 0;
  const loads: (() => void)[] = [];
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => `blob:${++next}`), revokeObjectURL: vi.fn() });
  vi.stubGlobal("Image", class {
    src = "";
    decode() { return new Promise<void>((resolve) => { loads.push(resolve); }); }
  });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  client.request("frame", { index: 0 });
  socket.onmessage?.({ data: JSON.stringify({ seq: 1, index: 0, kind: "draft", bytes: 2 }) });
  socket.onmessage?.({ data: new ArrayBuffer(2) });
  client.close();
  loads[0]!(); await Promise.resolve(); await Promise.resolve();
  expect(get(client.state).frame).toBeNull();
  expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1);
});

it("paints decoded cached pixels synchronously on every drag input, including repeated nearest frames", () => {
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:decoded"), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  const paint = vi.fn();
  client.onpaint = paint;
  try {
    client.request("recipe", { index: 2, recipe: { look: "neutral" } });
    socket.onmessage?.({ data: JSON.stringify({ seq: 1, index: 2, kind: "draft", bytes: 2 }) });
    socket.onmessage?.({ data: new ArrayBuffer(2) });
    for (const [n, index] of [3, 4, 5, 5].entries()) {
      client.scrub(index, { recipe: { look: "neutral" } });
      expect(paint).toHaveBeenCalledTimes(n + 1);
      const [images, header] = paint.mock.calls.at(-1)!;
      expect(images).toHaveLength(1);
      expect(images[0]).toBeInstanceOf(Image);
      expect(header.index).toBe(2);
    }
  } finally { client.close(); }
});

it("decodes prefetched playback frames before their audio deadline and presents them without another decode", async () => {
  const loads: (() => void)[] = [];
  let next = 0;
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => `blob:${++next}`), revokeObjectURL: vi.fn() });
  vi.stubGlobal("Image", class {
    src = "";
    decode() { return new Promise<void>((resolve) => { loads.push(resolve); }); }
  });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  try {
    client.request("frame", { index: 0, playback: true, ahead: 3 });
    socket.onmessage?.({ data: JSON.stringify({ seq: 1, index: 2, kind: "draft", bytes: 2, playback: true, buffer_end: true }) });
    socket.onmessage?.({ data: new ArrayBuffer(2) });
    expect(loads).toHaveLength(1);
    loads[0]!(); await Promise.resolve(); await Promise.resolve();
    expect(get(client.state).frame).toBeNull();
    client.present(2);
    expect(get(client.state)).toMatchObject({ frame: "blob:1", header: { index: 2 } });
    expect(loads).toHaveLength(1);
    expect(client.buffered).toBe(0);
  } finally { client.close(); }
});

it("keeps one source stream generation and advances it with clock credits", () => {
  let serial = 0;
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => `blob:${++serial}`), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  client.sourceResolution = true;
  const seq = client.request("recipe", { index: 0, recipe: { look: "neutral" }, playback: true, ahead: 3 });
  expect(JSON.parse(socket.send.mock.calls[0]![0])).toMatchObject({ source: true, seq });
  const reply = (index: number) => {
    socket.onmessage?.({ data: JSON.stringify({ seq, index, kind: "draft", playback: true, stream: true, source: true, width: 3840, height: 2160, bytes: 1 }) });
    socket.onmessage?.({ data: new ArrayBuffer(1) });
  };
  reply(0); reply(1); reply(2); reply(3); reply(4);
  client.present(1);
  expect(get(client.state).header?.index).toBe(1);
  expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ op: "advance", stream_seq: seq, index: 1 });
  client.request("recipe", { index: 2, recipe: { look: "neutral" }, playback: true, ahead: 3 });
  expect(socket.send.mock.calls.map(([data]) => JSON.parse(data).op)).toEqual(["recipe", "advance", "advance"]);
  client.present(2);
  expect(get(client.state).header?.index).toBe(2);
  const seek = client.request("frame", { index: 96 });
  reply(3);
  expect(get(client.state).header?.index).toBe(2);
  socket.onmessage?.({ data: JSON.stringify({ seq: seek, index: 96, kind: "draft", source: true, bytes: 1 }) });
  socket.onmessage?.({ data: new ArrayBuffer(1) });
  expect(get(client.state).header?.index).toBe(96);
  client.close();
});

it("a source pause requests a source frame and scrubbing remains a small draft", () => {
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  client.sourceResolution = true;
  client.request("refine", { index: 12 });
  expect(JSON.parse(socket.send.mock.calls[0]![0])).toMatchObject({ op: "frame", source: true, index: 12 });
  client.scrub(20, {});
  expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ scrub: true, source: false });
  client.close();
});

const displayLease = { name: "mcrawsome-preview-12-test", token: "a".repeat(32), slot: 0, slots: 5, stride: 192, width: 8, height: 4 };
it("keeps the completed native picture through pause until its replacement is decoded and painted", async () => {
  const socket = new Socket(), order: string[] = [];
  let decode!: () => void;
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:paused"), revokeObjectURL: vi.fn() });
  vi.stubGlobal("Image", class { src = ""; decode() { return new Promise<void>(resolve => { decode = resolve; }); } });
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  const hide = vi.fn(() => order.push("hide"));
  client.nativeOutput = { paint: vi.fn(async () => true), hide };
  client.sourceResolution = true;
  const playback = client.request("frame", { index: 0, playback: true });
  socket.onmessage?.({ data: JSON.stringify({ kind: "draft", seq: playback, index: 0, source: true, stream: true, playback: true, bytes: 0, display: displayLease }) });
  await vi.waitFor(() => expect(get(client.state).header?.display).toEqual(displayLease));
  hide.mockClear();order.length = 0;client.onpaint = () => order.push("paint");
  const paused = client.request("refine", { index: 0 });
  expect(hide).not.toHaveBeenCalled();
  socket.reply(paused);
  expect(hide).not.toHaveBeenCalled();
  expect(get(client.state).header?.display).toEqual(displayLease);
  decode();
  await vi.waitFor(() => expect(get(client.state).frame).toBe("blob:paused"));
  expect(order).toEqual(["paint", "hide"]);
  client.close();
});
it("holds native credit until paint completes without creating JPEG URLs", async () => {
  const socket = new Socket();
  vi.stubGlobal("URL", { createObjectURL: vi.fn(), revokeObjectURL: vi.fn() });
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  let finish!: (painted: boolean) => void;
  client.nativeOutput = { paint: vi.fn(() => new Promise<boolean>(resolve => { finish = resolve; })), hide: vi.fn() };
  client.sourceResolution = true;
  const seq = client.request("frame", { index: 0, playback: true });
  expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ native: true, source: true });
  socket.onmessage?.({ data: JSON.stringify({ kind: "draft", seq, index: 0, source: true, stream: true, playback: true, bytes: 0, display: displayLease }) });
  expect(client.nativeOutput!.paint).toHaveBeenCalledOnce();
  expect(socket.send.mock.calls.some(([text]) => JSON.parse(text).op === "release")).toBe(false);
  finish(true); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  expect(get(client.state).header?.index).toBe(0);
  expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ op: "release", display: displayLease });
  expect(URL.createObjectURL).not.toHaveBeenCalled();
  client.close();
});
it("releases old native leases and restarts a backward playback seek", () => {
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  client.nativeOutput = { paint: vi.fn(async () => true), hide: vi.fn() };
  client.sourceResolution = true;
  const first = client.request("frame", { index: 20, playback: true });
  const second = client.request("frame", { index: 2, playback: true, seek: true });
  expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ op: "frame", index: 2, seq: second });
  socket.onmessage?.({ data: JSON.stringify({ kind: "draft", seq: first, index: 21, stream: true, playback: true, bytes: 0, display: displayLease }) });
  expect(client.nativeOutput!.paint).not.toHaveBeenCalled();
  expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ op: "release", display: displayLease });
  client.close();
});

it("falls back to source JPEG when a native draw fails and returns every lease", async () => {
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:fallback"), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  client.nativeOutput = { paint: vi.fn().mockResolvedValue(false), hide: vi.fn() };
  client.sourceResolution = true;
  const seq = client.request("recipe", { recipe: {}, index: 0, playback: true });
  for (const index of [0, 1, 2]) socket.onmessage?.({ data: JSON.stringify({ kind: "draft", seq, index, source: true, stream: true, playback: true, bytes: 0, display: { ...displayLease, slot: index } }) });
  await vi.waitFor(() => expect(client.nativeOutput).toBeNull());
  const sent = socket.send.mock.calls.map(([value]) => JSON.parse(value));
  expect(sent.filter(value => value.op === "release")).toHaveLength(3);
  expect(sent.at(-2)?.native === false || sent.at(-1)?.native === false).toBe(true);
  const fallback = sent.filter(value => value.op === "recipe").at(-1);
  expect(fallback).toMatchObject({ source: true, native: false, playback: true });
  client.close();
});

it("ignores a completed native draw after a manual seek supersedes it", async () => {
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:seek"), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  let finish!: (value: boolean) => void;
  client.nativeOutput = { paint: () => new Promise<boolean>(resolve => { finish = resolve; }), hide: vi.fn() };
  client.sourceResolution = true;
  const seq = client.request("recipe", { recipe: {}, index: 20, playback: true });
  socket.onmessage?.({ data: JSON.stringify({ kind: "draft", seq, index: 20, source: true, stream: true, playback: true, bytes: 0, display: displayLease }) });
  const seek = client.request("frame", { index: 2 });
  socket.onmessage?.({ data: JSON.stringify({ kind: "draft", seq: seek, index: 2, bytes: 1 }) });
  socket.onmessage?.({ data: new ArrayBuffer(1) });
  finish(true);
  await vi.waitFor(() => expect(socket.send.mock.calls.some(([value]) => JSON.parse(value).op === "release")).toBe(true));
  expect(get(client.state).header?.index).toBe(2);
  client.close();
});

it("bounds native bridge calls and ignores skipped or out-of-order draw acknowledgements", async () => {
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  const pending: ((value: boolean | null) => void)[] = [];
  const paint = vi.fn(() => new Promise<boolean | null>(resolve => pending.push(resolve)));
  client.nativeOutput = { paint, hide: vi.fn() };
  client.sourceResolution = true;
  const seq = client.request("recipe", { recipe: {}, index: 0, playback: true });
  for (let index = 0; index < 5; index++) {
    client.present(index);
    socket.onmessage?.({ data: JSON.stringify({ kind: "draft", seq, index, source: true, stream: true, playback: true, bytes: 0, display: { ...displayLease, slot: index } }) });
  }
  expect(paint).toHaveBeenCalledTimes(3);
  pending[2]!(true);
  await vi.waitFor(() => expect(get(client.state).header?.index).toBe(2));
  pending[1]!(null); pending[0]!(true);
  await Promise.resolve(); await Promise.resolve();
  expect(get(client.state).header?.index).toBe(2);
  expect(client.nativeOutput).not.toBeNull();
  for (const finish of pending.slice(3)) finish(null);
  client.close();
});

it("streams Adaptive playback when the engine offers it, and asks for drafts in turn when it does not", () => {
  let serial = 0;
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => `blob:${++serial}`), revokeObjectURL: vi.fn() });
  const open = (supports: boolean) => {
    const socket = new Socket();
    const client = new PreviewClient("a", () => socket as unknown as WebSocket);
    socket.onmessage?.({ data: JSON.stringify({ kind: "opened", frame_count: 100, rate: 24, supports_stream: supports }) });
    return { socket, client };
  };
  const streamed = open(true);
  const seq = streamed.client.request("recipe", { index: 0, recipe: { look: "neutral" }, playback: true, ahead: 3 });
  expect(JSON.parse(streamed.socket.send.mock.calls[0]![0])).toMatchObject({ stream: true, native: false, playback: true, seq });
  expect(JSON.parse(streamed.socket.send.mock.calls[0]![0]).source).toBeUndefined();
  for (const index of [0, 1, 2]) {
    streamed.socket.onmessage?.({ data: JSON.stringify({ seq, index, kind: "draft", playback: true, stream: true, width: 1280, height: 720, bytes: 1 }) });
    streamed.socket.onmessage?.({ data: new ArrayBuffer(1) });
  }
  streamed.client.request("recipe", { index: 1, recipe: { look: "neutral" }, playback: true, ahead: 3 });
  expect(get(streamed.client.state).header?.index).toBe(1);
  expect(streamed.socket.send.mock.calls.map(([data]) => JSON.parse(data).op)).toEqual(["recipe", "advance"]);
  streamed.client.request("refine", { index: 1 });  // pausing ends the stream
  expect(JSON.parse(streamed.socket.send.mock.calls.at(-1)![0])).toMatchObject({ op: "refine" });
  expect(JSON.parse(streamed.socket.send.mock.calls.at(-1)![0]).stream).toBeUndefined();
  streamed.client.close();

  const older = open(false);
  older.client.request("recipe", { index: 0, recipe: { look: "neutral" }, playback: true, ahead: 3 });
  const sent = JSON.parse(older.socket.send.mock.calls[0]![0]);
  expect(sent).toMatchObject({ ahead: 3, playback: true });
  expect(sent.stream).toBeUndefined();
  older.client.close();
});

it("streams Adaptive playback as native frames when the desktop surface is there", () => {
  const socket = new Socket();
  vi.stubGlobal("URL", { createObjectURL: vi.fn(), revokeObjectURL: vi.fn() });
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  socket.onmessage?.({ data: JSON.stringify({ kind: "opened", frame_count: 100, rate: 24, supports_stream: true }) });
  client.nativeOutput = { paint: vi.fn(() => Promise.resolve(true)), hide: vi.fn() };
  const seq = client.request("frame", { index: 0, playback: true });
  expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ stream: true, native: true, seq });
  socket.onmessage?.({ data: JSON.stringify({ kind: "draft", seq, index: 0, stream: true, playback: true, bytes: 0, display: displayLease }) });
  expect(client.nativeOutput!.paint).toHaveBeenCalledOnce();
  expect(URL.createObjectURL).not.toHaveBeenCalled();
  client.close();
});

it("keeps streamed pictures moving when decode takes longer than the audio frame interval", () => {
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:slow"), revokeObjectURL: vi.fn() });
  const socket = new Socket();
  const client = new PreviewClient("a", () => socket as unknown as WebSocket);
  socket.onmessage?.({ data: JSON.stringify({ kind: "opened", supports_stream: true, frame_count: 240, rate: 24 }) });
  const seq = client.request("frame", { index: 0, playback: true });
  const reply = (index: number) => {
    socket.onmessage?.({ data: JSON.stringify({ seq, index, kind: "draft", stream: true, playback: true, bytes: 1 }) });
    socket.onmessage?.({ data: new ArrayBuffer(1) });
  };
  client.present(24);
  reply(10);
  expect(get(client.state).header?.index).toBe(10);
  client.present(30);
  reply(20);
  expect(get(client.state).header?.index).toBe(20);
  reply(15);
  expect(get(client.state).header?.index).toBe(20);
  client.request("frame", { index: 0 });
  reply(25);
  expect(get(client.state).header?.index).toBe(20);
  client.close();
});
