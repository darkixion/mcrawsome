// The engine's event stream (one EventSource on /api/v1/events) as Svelte stores.
import { get, writable } from "svelte/store";
import { api, type ApiError } from "./api";
import { receive, reloadLibrary, type LibraryChange } from "./library";

export type EngineState = "connected" | "reconnecting" | "stopped";

export const EVENTS = "/api/v1/events";
const FIRST_DELAY_MS = 500;
const LONGEST_DELAY_MS = 10_000;
const STOPPED_AFTER_MS = 5000;
const CLOSED = 2; // EventSource.CLOSED: the browser has given up on this stream

export const mediaUse = writable<Record<string, boolean | null>>({});

export const analysis = writable<{ id: string; report?: Record<string, unknown>; error?: ApiError } | null>(null);

/** `event: send`: a stage of the send the page tagged `send`, for one of its clips (`mount-<id>`)
 * or for the whole send (null). Event bodies are not in the OpenAPI document. */
export type SendStage = { send: string; mount: string | null; stage: string };
export const sendStage = writable<SendStage | null>(null);

export const engine = writable<EngineState>("reconnecting");

/** Follow the event stream until the returned function is called. The browser retries a dropped
 * stream by itself; when it gives up, a new one is opened after a delay that doubles each time. */
export function connect(create: (url: string) => EventSource = (url) => new EventSource(url)): () => void {
  let source: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let healthTimer: ReturnType<typeof setTimeout> | null = null;
  let activity = 0;
  let failures = 0;
  let stopped = false;
  const checkHealth = async (): Promise<void> => {
    healthTimer = null;
    const since = activity;
    const result = await api.health();
    if (stopped || activity !== since) return;
    if (!result.ok) engine.set("stopped");
    else if (get(engine) === "stopped") engine.set("reconnecting");
    healthTimer = setTimeout(() => void checkHealth(), STOPPED_AFTER_MS);
  };

  const reached = (): void => {
    if (stopped) return;
    if (failures > 0) { // catch up on what was missed while away
      reloadLibrary();
    }
    failures = 0;
    activity += 1;
    if (healthTimer !== null) clearTimeout(healthTimer);
    healthTimer = setTimeout(() => void checkHealth(), STOPPED_AFTER_MS);
    engine.set("connected");
  };

  const open = (): void => {
    if (stopped) return;
    source = create(EVENTS);
    source.addEventListener("open", reached);
    source.addEventListener("library", (event) => {
      if (stopped) return;
      reached();
      receive(JSON.parse((event as MessageEvent<string>).data) as LibraryChange);
    });
    source.addEventListener("media-use", (event) => {
      if (stopped) return;
      reached();
      mediaUse.set(JSON.parse((event as MessageEvent<string>).data));
    });
    source.addEventListener("analysis", (event) => {
      if (stopped) return;
      reached();
      analysis.set(JSON.parse((event as MessageEvent<string>).data));
    });
    source.addEventListener("send", (event) => {
      if (stopped) return;
      reached();
      sendStage.set(JSON.parse((event as MessageEvent<string>).data) as SendStage);
    });
    source.addEventListener("message", reached);
    source.addEventListener("error", () => {
      if (stopped) return;
      failures += 1;
      if (failures === 1) {
        activity += 1;
        if (healthTimer !== null) clearTimeout(healthTimer);
        engine.set("reconnecting");
        healthTimer = setTimeout(() => void checkHealth(), STOPPED_AFTER_MS);
      }
      if (source?.readyState !== CLOSED || stopped) return;
      source.close();
      const delay = Math.min(FIRST_DELAY_MS * 2 ** (failures - 1), LONGEST_DELAY_MS);
      timer = setTimeout(open, delay);
    });
  };

  engine.set("reconnecting");
  activity += 1;
  healthTimer = setTimeout(() => void checkHealth(), STOPPED_AFTER_MS);
  open();
  return () => {
    stopped = true;
    if (timer !== null) clearTimeout(timer);
    if (healthTimer !== null) clearTimeout(healthTimer);
    source?.close();
  };
}
