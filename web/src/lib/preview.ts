// Headers and payloads arrive as separate WebSocket messages. Auxiliary replies never advance the frame.
import { get, writable } from "svelte/store";

export type DisplayFrame = { name: string; slot: number; token: string; stride: number; slots: number; width: number; height: number };
export type NativeOutput = { paint: (header: Header, epoch: number) => Promise<boolean | null>; hide: (epoch: number) => void };
export type Header = {
  display?: DisplayFrame;
  stream?: boolean; source?: boolean; supports_source?: boolean; supports_stream?: boolean;
  buffer_end?: boolean; playback?: boolean; purpose?: string; kind: string; seq?: number; bytes?: number; ms?: number; index?: number;
  backend?: string; backend_reason?: string; timings?: Record<string, number>;
  width?: number; height?: number; size?: number; frame_count?: number; rate?: number;
  frame_width?: number; frame_height?: number;
  as_shot?: { temperature: number | null; tint: number | null };
  exposure?: { auto: boolean; stops: number | null; measuring?: boolean };
  error?: { code: string; message: string }; notes?: string[];
};
export type PreviewState = { frame: string | null; kind: string; ms: number; header: Header | null; problem: Header["error"] | null };
export type Op = "recipe" | "frame" | "refine" | "linear16" | "advance" | "release";

type Request = { seq: number; op: Op; args: Record<string, unknown> };
type Cached = { header: Header; frame: string; images: HTMLImageElement[]; bytes: number; ready: boolean; released: boolean };

export class PreviewClient {
  nativeOutput: NativeOutput | null = null;
  private nativeEpoch = 0;
  private nativePainting = new Set<Cached>();
  private resetNative(epoch: number, hide = true): void {
    this.nativeEpoch = epoch;
    if (hide) this.nativeOutput?.hide(epoch);
    this.dropBuffer();
  }
  private dropBuffer(): void {
    for (const frame of this.buffer) if (frame.header.display) this.release(frame);
    this.buffer = [];
  }
  private releaseDisplay(header: Header): void {
    if (header.display && !this.disconnected && !this.ended && !this.replaced)
      this.send({ seq: ++this.seq, op: "release", args: { display: header.display } });
  }

  private source = false;
  private streams = false;  // the engine streams Adaptive playback (older engines answer `ahead` requests)
  get sourceResolution(): boolean { return this.source; }
  set sourceResolution(value: boolean) {
    if (value === this.source) return;
    this.source = value;
    this.resetNative(++this.seq);
    this.stream = null; this.streamClock = -1;
    this.playback = null; this.queuedPlayback = null;
    this.buffer = []; this.lastIndex = -1;
    this.clearCache();
  }
  readonly state = writable<PreviewState>({ frame: null, kind: "opening", ms: 0, header: null, problem: null });
  onopened: ((header: Header) => void) | null = null;
  onframe: ((header: Header) => void) | null = null;
  onpaint: ((images: HTMLImageElement[], header: Header) => void) | null = null;
  opened: Header | null = null;
  private socket!: WebSocket;
  private readonly address: string;
  private readonly create: (url: string) => WebSocket;
  private disconnected = false;
  private replaced = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private attempts = 0;
  private playback: number | null = null;
  private stream: number | null = null;
  private streamClock = -1;
  private queuedPlayback: Request | null = null;
  private scrubbing: number | null = null;
  private queuedScrub: Request | null = null;
  private scrubTimer: ReturnType<typeof setTimeout> | null = null;
  private scrubIndex: number | null = null;
  private cacheKey = "";
  private cache = new Map<number, Cached>();
  private cacheBytes = 0;
  private exposureKey = "";
  private cacheVersion = 0;
  private decoding = new Set<Cached>();
  private resume: { op: Op; args: Record<string, unknown> } | null = null;
  private seq = 0;
  private newest = 0;
  private manualSeq = 0;
  private shown = 0;
  private announced: Header | null = null;
  private url: string | null = null;
  private clock = 0;
  private rate = 24;
  private lead = 0;
  private lastIndex = -1;
  private frameCount = Infinity;
  private buffer: Cached[] = [];
  get buffered(): number { return this.buffer.length; }
  private get cacheLimit(): number { return (this.sourceResolution ? 384 : 64) * 1024 * 1024; }
  private cacheReaders(): Set<Cached> {
    return new Set([...this.buffer, ...this.decoding, ...this.nativePainting]);
  }
  cacheUsage(): { estimated_bytes: number; limit_bytes: number; entries: number; reclaimable_entries: number } {
    const readers = this.cacheReaders();
    return { estimated_bytes: this.cacheBytes, limit_bytes: this.cacheLimit, entries: this.cache.size,
      reclaimable_entries: [...this.cache.values()].filter(frame => frame.frame !== this.url && !readers.has(frame)).length };
  }
  clearFrameCache(): void {
    // Keep pictures held by the display, playback clock and image decoders. In particular, do
    // not invalidate decode callbacks or return native display credit during maintenance.
    const readers = this.cacheReaders();
    for (const [index, frame] of this.cache) {
      if (frame.frame !== this.url && !readers.has(frame)) this.evict(index, frame);
    }
  }
  warmFrom: (() => number) | null = null;

  scrub(index: number, args: Record<string, unknown>): void {
    this.request("recipe", { ...args, index, fast: true, scrub: true });
    this.scrubIndex = index;
    if (this.scrubTimer !== null) clearTimeout(this.scrubTimer);
    this.scrubTimer = setTimeout(() => {
      this.scrubTimer = null;
      this.request("recipe", { ...args, index, fast: true, scrub: false });
    }, 120);
    this.showNearest(index);
  }

  private showNearest(index: number): boolean {
    let nearest: Cached | undefined;
    for (const cached of this.cache.values()) {
      if (!cached.ready) continue;
      if (!nearest || Math.abs(cached.header.index! - index) < Math.abs(nearest.header.index! - index)
        || (Math.abs(cached.header.index! - index) === Math.abs(nearest.header.index! - index) && cached.header.index! < nearest.header.index!)) nearest = cached;
    }
    if (nearest) {
      // Draw now, even when the same nearest frame was used by the preceding pointer move.
      this.onpaint?.(nearest.images, nearest.header);
      this.nativeOutput?.hide(this.nativeEpoch);
      this.releaseShown();
      this.url = nearest.frame;
      this.state.set({ frame: nearest.frame, kind: "cached", ms: 0, header: nearest.header, problem: null });
      this.onframe?.(nearest.header);
      return true;
    }
    return false;
  }

  present(index: number): void {
    this.clock = index;
    if (this.stream !== null && index !== this.streamClock && !this.disconnected && !this.ended) {
      this.streamClock = index;
      this.send({ seq: ++this.seq, op: "advance", args: { stream_seq: this.stream, index } });
    }
    const previous = this.buffer;
    this.buffer = this.buffer.filter((frame) => !frame.released && (frame.header.seq ?? 0) >= this.manualSeq
      && (frame.header.stream || frame.header.index! >= index - 1) && frame.header.index! > this.lastIndex);
    for (const frame of previous) if (frame.header.display && !this.buffer.includes(frame)) this.release(frame);
    const newest = this.buffer.filter((frame) => frame.ready && frame.header.index! <= index).at(-1);
    // Decoding starts on arrival. The audio clock selects only complete, due images.
    if (newest && this.nativePainting.size < 3) {
      for (const frame of this.buffer) if (frame !== newest && frame.header.display && frame.header.index! <= newest.header.index!) this.release(frame);
      this.display(newest);
      this.buffer = this.buffer.filter((frame) => frame.header.index! > newest.header.index!);
    }
  }
  private ended = false;

  constructor(id: string, create: (url: string) => WebSocket = (url) => new WebSocket(url)) {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    this.address = `${protocol}//${location.host}/api/v1/clips/${encodeURIComponent(id)}/preview`;
    this.create = create;
    this.connect();
  }

  private connect(): void {
    if (this.ended || this.replaced) return;
    let socket: WebSocket;
    try { socket = this.create(this.address); }
    catch { this.reconnect(); return; }
    this.socket = socket;
    socket.binaryType = "arraybuffer";
    socket.onmessage = (event: MessageEvent<string | ArrayBuffer>) => {
      if (this.ended || socket !== this.socket) return;
      if (typeof event.data === "string") {
        const header = JSON.parse(event.data) as Header;
        if (header.bytes) this.announced = header;
        else this.arrived(header, new ArrayBuffer(0));
      } else if (this.announced !== null) {
        const header = this.announced;
        this.announced = null;
        this.arrived(header, event.data);
      }
    };
    socket.onclose = (event) => {
      if (this.ended || socket !== this.socket || this.replaced) return;
      if (event?.code === 4000) { this.replace(); return; }
      this.reconnect();
    };
  }

  private replace(): void {
    this.resetNative(++this.seq);
    this.replaced = true;
    this.state.update((state) => ({ ...state, problem: { code: "preview-replaced", message: "This preview is open in another window." } }));
    this.socket.close();
  }

  private reconnect(): void {
    if (this.ended || this.replaced || this.retryTimer !== null) return;
    this.disconnected = true;
    this.resetNative(++this.seq);
    this.opened = null;
    this.announced = null;
    this.buffer = []; this.lastIndex = -1; this.lead = 0;
    this.playback = null;
    this.stream = null; this.streamClock = -1;
    this.queuedPlayback = null;
    this.cancelScrub(); this.clearCache();
    this.state.update((state) => ({ ...state, problem: { code: "preview-reconnecting", message: "Preview disconnected. Reconnecting..." } }));
    const delay = Math.min(5000, 250 * 2 ** Math.min(this.attempts++, 5));
    this.retryTimer = setTimeout(() => { this.retryTimer = null; this.connect(); }, delay);
  }

  request(op: Op, args: Record<string, unknown> = {}): number {
    if ((op === "recipe" || op === "frame") && args.playback !== true && args.scrub !== true && this.warmFrom) {
      args = { ...args, warm_from: this.warmFrom() };
    }
    if (this.sourceResolution && ["recipe", "frame", "refine"].includes(op)) {
      if (op === "refine") op = "frame";
      args = { ...args, source: args.scrub !== true, native: args.playback === true && args.scrub !== true && this.nativeOutput !== null };
    } else if (this.streams && args.playback === true && args.scrub !== true && (op === "recipe" || op === "frame")) {
      // An engine that streams Adaptive drafts plays them as Source frames are played.
      args = { ...args, stream: true, native: this.nativeOutput !== null };
    }
    const request = { seq: ++this.seq, op, args };
    if (args.recipe !== undefined) {
      const key = JSON.stringify(args.recipe);
      if (key !== this.cacheKey) {
        this.resetNative(request.seq);
        this.cancelScrub(); this.clearCache(); this.cacheKey = key;
        this.stream = null; this.streamClock = -1;
        this.playback = null; this.queuedPlayback = null;
        this.buffer = []; this.lastIndex = -1;
      }
    }
    if (args.scrub !== true) this.cancelScrub();
    if (op === "recipe" || op === "frame") this.resume = { op, args };
    if (this.ended || this.replaced || this.disconnected) return request.seq;
    if (args.seek === true) {
      this.resetNative(request.seq); this.stream = null; this.streamClock = -1;
      this.playback = null; this.queuedPlayback = null; this.lastIndex = -1;
    }
    const playback = args.playback === true && (op === "recipe" || op === "frame");
    if (playback && (args.source === true || args.stream === true)) {
      if (this.stream !== null) { this.present(Number(args.index)); return request.seq; }
      this.nativeEpoch = request.seq;
      this.stream = request.seq; this.streamClock = Number(args.index);
      this.clock = Number(args.index); this.buffer = []; this.lastIndex = -1;
      this.send(request);
      return request.seq;
    }
    if (playback && args.ahead === 3) {
      this.present(Number(args.index));
      if (this.buffer.length >= 2) return request.seq;
      const last = this.buffer.at(-1)?.header.index ?? this.lastIndex;
      if (last >= this.frameCount - 1) return request.seq;
      request.args = { ...args, index: Math.max(Number(args.index), last + 1) };
    }
    if (playback && this.playback !== null) {
      this.queuedPlayback = request;
      return request.seq;
    }
    if (!playback && op !== "release") {
      // A pause invalidates streamed leases, but the last completed native picture stays up until
      // its paused/refined replacement is decoded. Hiding here exposed a blank canvas for seconds.
      this.resetNative(request.seq, false);
      this.stream = null; this.streamClock = -1;
      this.playback = null;
      this.queuedPlayback = null;
      this.buffer = []; this.lastIndex = -1;
    }
    if (args.scrub === true && this.scrubbing !== null) { this.queuedScrub = request; return request.seq; }
    this.send(request);
    return request.seq;
  }

  private send(request: Request): void {
    if (request.args.ahead === 3 && request.args.playback === true && request.args.source !== true) {
      const last = this.buffer.at(-1)?.header.index ?? this.lastIndex;
      if (this.buffer.length >= 2 || last >= this.frameCount - 1) return;
      request.args = { ...request.args, index: Math.min(this.frameCount - 1, Math.max(this.clock + this.lead, Number(request.args.index), last + 1)), ahead: 3 - this.buffer.length };
    }
    const { seq, op, args } = request;
    if (!["advance", "release"].includes(op)) this.newest = seq;
    if (!["advance", "release"].includes(op) && args.playback !== true) this.manualSeq = seq;
    if (args.playback === true && (op === "recipe" || op === "frame")) this.playback = seq;
    if (args.scrub === true) this.scrubbing = seq;
    try { this.socket.send(JSON.stringify({ ...args, op, seq })); }
    catch { this.socket.close(); this.reconnect(); }
  }

  private arrived(header: Header, data: ArrayBuffer): void {
    if (header.display && (header.seq !== this.stream || header.index! <= this.lastIndex)) {
      this.releaseDisplay(header); return;
    }
    if (header.kind === "error" && header.seq === this.stream && this.nativeOutput) { this.nativeFallback(); return; }
    if (header.stream && header.seq === this.stream) {
      if (header.index! > this.lastIndex) this.show(header, data, true);
    } else if (header.playback && header.buffer_end !== undefined && (header.seq ?? 0) >= this.newest) {
      if (header.ms !== undefined) this.lead = Math.min(3, Math.ceil((header.ms + 12) * this.rate / 1000));
      if (header.index! > this.lastIndex && header.index! >= this.clock - 1) {
        this.show(header, data, true);
      }
    } else this.deliver(header, data);
    if (header.seq === this.scrubbing && ["draft", "error"].includes(header.kind)) {
      this.scrubbing = null;
      const next = this.queuedScrub; this.queuedScrub = null;
      if (next) this.send(next);
    }
    if (header.seq === this.playback && ["draft", "error"].includes(header.kind) && header.buffer_end !== false) {
      this.playback = null;
      const next = this.queuedPlayback;
      this.queuedPlayback = null;
      if (next) this.send(next);
    }
  }

  private deliver(header: Header, data: ArrayBuffer): void {
    const seq = header.seq ?? 0;
    if (header.kind === "opened") {
      this.opened = header;
      this.rate = header.rate ?? 24;
      this.frameCount = header.frame_count ?? Infinity;
      this.streams = header.supports_stream === true;
      const reconnecting = this.disconnected;
      this.disconnected = false; this.attempts = 0;
      this.state.update((state) => ({ ...state, problem: null }));
      const before = this.seq;
      this.onopened?.(header);
      if (reconnecting && this.seq === before && this.resume) this.request(this.resume.op, { ...this.resume.args, scrub: false });
      return;
    }
    if (header.kind === "closed") { this.replace(); return; }
    if (header.kind === "error") {
      if ((!header.purpose || header.purpose === "frame") && (seq === 0 || seq >= this.newest)) this.state.update((state) => ({ ...state, problem: header.error ?? null }));
      return;
    }
    if (!["draft", "refined"].includes(header.kind) || seq < this.newest || seq < this.shown) return;
    this.show(header, data);
  }

  private show(header: Header, data: ArrayBuffer, buffered = false): void {
    if (header.display) {
      const cached: Cached = { header, frame: "", images: [], bytes: 0, ready: true, released: false };
      this.buffer.push(cached);
      this.buffer.sort((a,b) => a.header.index! - b.header.index!);
      while (this.buffer.length > 5) this.release(this.buffer.shift()!);
      this.present(this.clock);
      return;
    }
    if (header.exposure) {
      const key = JSON.stringify(header.exposure);
      if (key !== this.exposureKey) { this.clearCache(); this.exposureKey = key; }
    }
    const version = this.cacheVersion;
    const frame = URL.createObjectURL(new Blob([data], { type: "image/jpeg" }));
    const image = new Image(); image.src = frame;
    const images = [image];
    const cached: Cached = { header, frame, images, bytes: data.byteLength + (header.width ?? 1920) * (header.height ?? 1080) * 4, ready: false, released: false };
    if (header.index !== undefined) {
      const old = this.cache.get(header.index);
      if (old) this.evict(header.index, old);
      this.cache.set(header.index, cached); this.cacheBytes += cached.bytes;
      while (this.cache.size > 24 || this.cacheBytes > this.cacheLimit) {
        const [index, removed] = this.cache.entries().next().value!;
        this.evict(index, removed);
      }
    }
    if (buffered) {
      this.buffer.push(cached);
      this.buffer.sort((a, b) => a.header.index! - b.header.index!);
      this.buffer = this.buffer.slice(this.sourceResolution ? -5 : this.stream !== null ? -7 : -3);
    }
    const ready = () => {
      this.decoding.delete(cached);
      if (cached.released || this.ended || version !== this.cacheVersion) { this.release(cached); return; }
      cached.ready = true;
      if (buffered) this.present(this.clock);
      else this.display(cached);
    };
    const decodes = images.filter((image) => typeof image.decode === "function").map((image) => image.decode());
    if (!decodes.length) { ready(); return; }
    this.decoding.add(cached);
    void Promise.all(decodes).then(ready, () => {
      this.decoding.delete(cached);
      if (header.index !== undefined && this.cache.get(header.index) === cached) this.evict(header.index, cached);
      else this.release(cached);
    });
  }

  private updateTiming(header: Header): void {
    if (header.rate !== undefined && header.rate > 0) this.rate = header.rate;
    if (header.frame_count !== undefined && header.frame_count > 0) this.frameCount = header.frame_count;
  }

  private display(cached: Cached): void {
    const { header, frame } = cached;
    if (header.display) { this.paintNative(cached); return; }
    // A drag may advance while decoding. Its nearest ready image is still useful.
    if (this.scrubIndex !== null && this.showNearest(this.scrubIndex)) return;
    const seq = header.seq ?? 0;
    if (header.playback && (header.index! < this.lastIndex || (!header.stream && header.index! < this.clock - 1))) return;
    // Requesting ahead keeps due images valid; manual input supersedes them.
    if (header.playback ? seq < this.manualSeq : seq < this.newest || seq < this.shown) {
      if (header.index === undefined) this.release(cached);
      return;
    }
    this.updateTiming(header);
    if (get(this.state).header?.display) this.onpaint?.(cached.images, header);
    this.nativeOutput?.hide(this.nativeEpoch);
    this.shown = seq; this.lastIndex = header.index ?? this.lastIndex;
    this.releaseShown();
    this.url = frame;
    this.state.set({ frame, kind: header.kind, ms: header.ms ?? 0, header, problem: null });
    this.onframe?.(header);
  }

  private paintNative(cached: Cached): void {
    const epoch = this.nativeEpoch, output = this.nativeOutput;
    if (!output) { this.release(cached); this.nativeFallback(); return; }
    this.nativePainting.add(cached);
    void output.paint(cached.header, epoch).then((painted) => {
      if (this.ended || epoch !== this.nativeEpoch) return;
      if (painted === false) { this.nativeFallback(); return; }
      if (painted === null || cached.header.index! <= this.lastIndex) return;
      const header = cached.header;
      this.updateTiming(header);
      this.shown = header.seq ?? this.shown; this.lastIndex = header.index ?? this.lastIndex;
      this.state.set({ frame: this.url, kind: header.kind, ms: header.ms ?? 0, header, problem: null });
      this.onframe?.(header);
    }, () => { if (!this.ended && epoch === this.nativeEpoch) this.nativeFallback(); }).finally(() => {
      this.release(cached);
      this.nativePainting.delete(cached);
      if (!this.ended && this.stream !== null) this.present(this.clock);
    });
  }
  private nativeFallback(): void {
    this.resetNative(++this.seq);
    this.nativeOutput = null;
    this.stream = null; this.playback = null; this.lastIndex = -1;
    if (this.resume) this.request(this.resume.op, { ...this.resume.args, index: this.clock });
  }

  private cancelScrub(): void {
    this.scrubIndex = null;
    this.scrubbing = null; this.queuedScrub = null;
    if (this.scrubTimer !== null) clearTimeout(this.scrubTimer);
    this.scrubTimer = null;
  }
  private releaseShown(): void {
    if ([...this.cache.values()].some((cached) => cached.frame === this.url)) return;
    if (this.url) URL.revokeObjectURL(this.url);
  }
  private evict(index: number, cached: Cached): void {
    this.cache.delete(index); this.cacheBytes -= cached.bytes;
    if (cached.frame !== this.url) this.release(cached);
  }
  private release(cached: Cached): void {
    if (cached.released) return;
    cached.released = true;
    if (cached.header.display) { this.releaseDisplay(cached.header); return; }
    URL.revokeObjectURL(cached.frame);
  }
  private clearCache(): void {
    ++this.cacheVersion;
    for (const [index, cached] of this.cache) this.evict(index, cached);
    for (const cached of this.decoding) this.release(cached);
    this.decoding.clear();
  }

  close(): void {
    if (this.ended) return;
    this.resetNative(++this.seq);
    this.ended = true;
    this.cancelScrub(); this.clearCache();
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.playback = null; this.queuedPlayback = null;
    this.stream = null; this.streamClock = -1;
    this.socket?.close();
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = null; this.buffer = [];
  }
}
