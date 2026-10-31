// RAW handoffs share one action and per-target history, independent of the receiving application.
import { get, writable } from "svelte/store";
import { api, type Result, type Schemas } from "./api";
import { bridge } from "./bridge";
import { sendStage } from "./events";

export type SendTarget = {
  id: string; label: string;
  mode?: "bin" | "timeline";
  send: (mounts: string[], progress?: string) => Promise<Result<Schemas["NativeSendResult"]>>;
  disabled: () => string | null;
};
export const sendTargets: readonly SendTarget[] = [{
  id: "resolve", label: "Resolve (Media Bin)", send: (mounts, progress) => api.sendMountsToResolve(mounts, "bin", progress),
  disabled: () => null,
}];
// Modes share their destination identity, history and mount lifetime. A timeline stabilises each clip
// whose Stabilise switch is on (Develop), so there is no separate gyro choice.
export const sendChoices: readonly SendTarget[] = [
  ...sendTargets,
  { ...sendTargets[0]!, mode: "timeline", label: "Resolve (Timeline)",
    send: (mounts, progress) => api.sendMountsToResolve(mounts, "timeline", progress) },
];
export const sendKey = (target: SendTarget): string => `${target.id}:${target.mode ?? "bin"}`;
export const preferredTarget = writable("resolve");
export type SendClip = { id: string; name: string };
export type SendEntry = SendClip & {
  target: string; targetLabel: string; state: "sending" | "sent" | "failed";
  message: string; archive: string | null; at: string;
  preparation?: "queued" | "mounting" | "ready" | "failed";
};
export type SendActivity = { target: string; targetLabel: string; busy: boolean; phase?: "mounting" | "dispatching"; stage?: string; entries: SendEntry[] };
export const sendHistory = writable<SendEntry[]>([]);
export const sendActivity = writable<SendActivity | null>(null);
const STORAGE = "mcrawsome.raw-sends.v1";
const LIMIT = 256;

function valid(value: unknown): value is SendEntry {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<SendEntry>;
  return [item.id, item.name, item.target, item.targetLabel, item.message, item.at].every(value => typeof value === "string" && value.length <= 8192)
    && (item.state === "sent" || item.state === "failed") && (item.archive === null || typeof item.archive === "string");
}
export function restoreSendHistory(): void {
  try {
    const raw = localStorage.getItem(STORAGE);
    if (!raw || raw.length > 2_000_000) return;
    const items: unknown = JSON.parse(raw);
    if (!Array.isArray(items)) return;
    const merged = [...items.filter(valid), ...get(sendHistory)];
    sendHistory.set([...new Map(merged.map(item => [`${item.id}:${item.target}`, item])).values()].slice(-LIMIT));
  } catch { /* History is optional when browser storage is unavailable. */ }
}
function remember(entries: SendEntry[]): void {
  sendHistory.update(previous => {
    const keys = new Set(entries.map(entry => `${entry.id}:${entry.target}`));
    const result = [...previous.filter(entry => !keys.has(`${entry.id}:${entry.target}`)), ...entries].slice(-LIMIT);
    try { localStorage.setItem(STORAGE, JSON.stringify(result)); } catch { /* Sending still succeeds. */ }
    return result;
  });
}
export function clipSends(id: string, history: SendEntry[], activity: SendActivity | null): SendEntry[] {
  const entries = [...history.filter(entry => entry.id === id), ...(activity?.entries.filter(entry => entry.id === id) ?? [])];
  return [...new Map(entries.map(entry => [entry.target, entry])).values()];
}

// A send that finishes while the window is not in front (a stabilised timeline can take minutes) says so in
// a desktop notification: through the desktop window, or a browser's own once allowed (never asked for).
function announce(target: SendTarget, entries: SendEntry[]): void {
  if (document.visibilityState === "visible" && document.hasFocus()) return;
  const sent = entries.filter(entry => entry.state === "sent").length;
  const failed = entries.find(entry => entry.state === "failed");
  const clips = (count: number) => `${count} ${count === 1 ? "clip" : "clips"}`;
  const summary = !failed ? `${clips(sent)} sent to ${target.label}`
    : sent ? `${sent} of ${clips(entries.length)} sent to ${target.label}` : `Send to ${target.label} failed`;
  const body = !failed ? `Complete the import in ${target.id === "resolve" ? "Resolve" : "the receiving application"}.`
    : sent ? `${failed.name}: ${failed.message}` : failed.message;
  const desktop = get(bridge)?.api.notify;
  if (desktop) void desktop(summary, body).catch(() => {});
  else if (typeof Notification !== "undefined" && Notification.permission === "granted") new Notification(summary, { body });
}

export async function sendRaw(clips: SendClip[], target: SendTarget, onmounted: (mount?: Schemas["MountedSequence"]) => void | Promise<void>): Promise<void> {
  if (get(sendActivity)?.busy || clips.length === 0) return;
  const entries: SendEntry[] = [...new Map(clips.map(clip => [clip.id, clip])).values()].map(clip => ({
    ...clip, target: target.id, targetLabel: target.label, state: "sending", preparation: "queued", message: "Waiting to mount", archive: null, at: new Date().toISOString(),
  }));
  const activity: SendActivity = { target: target.id, targetLabel: target.label, busy: true, phase: "mounting", entries };
  const publish = () => sendActivity.set({ ...activity, entries: entries.map(entry => ({ ...entry })) });
  const fail = (message: string) => { for (const entry of entries) if (entry.state === "sending") { entry.state = "failed"; entry.preparation = "failed"; entry.message = message; } };
  publish();
  try {
    const disabled = target.disabled();
    if (disabled) { fail(disabled); return; }
    if (entries.length > 64) { fail("Select at most 64 RAW clips for a send"); return; }
    const listing = await api.mounts();
    if (!listing.ok) { fail(listing.message); return; }
    if (!listing.data.available) { fail(listing.data.reason); return; }
    const prepared: ({ entry: SendEntry; id: string } | undefined)[] = new Array(entries.length);
    let next = 0;
    async function mountNext(): Promise<void> {
      while (next < entries.length) {
        const index = next++;
        const entry = entries[index]!;
        entry.preparation = "mounting";
        entry.message = "Mounting RAW video and WAV audio";
        publish();
        try {
          const result = await api.mount(entry.id);
          if (result.ok) {
            prepared[index] = { entry, id: result.data.id };
            await onmounted(result.data);
            entry.preparation = "ready";
            entry.message = "Media ready";
          } else {
            entry.state = "failed";
            entry.preparation = "failed";
            entry.message = result.message;
          }
        } catch {
          entry.state = "failed";
          entry.preparation = "failed";
          entry.message = "Mount preparation was interrupted";
        }
        publish();
      }
    }
    await Promise.all(Array.from({ length: Math.min(4, entries.length) }, () => mountNext()));
    const mounted = prepared.filter((item): item is { entry: SendEntry; id: string } => item !== undefined);
    if (mounted.length === 0) return;
    activity.phase = "dispatching";
    for (const { entry } of mounted) entry.message = target.mode === "timeline" ? "Stabilising the clips set to Stabilise, then sending the timeline"
      : "Creating handoff and sending to the receiving application";
    publish();
    // The engine tells this send's stages on the event stream: each clip's in its entry, the latest in the summary.
    const token = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    const following = sendStage.subscribe(stage => {
      if (stage?.send !== token) return;
      const told = mounted.filter(({ id }) => stage.mount === `mount-${id}`);
      for (const { entry } of told) entry.message = stage.stage;
      activity.stage = told[0] && mounted.length > 1 ? `${told[0].entry.name}: ${stage.stage}` : stage.stage;
      publish();
    });
    const result = await target.send([...new Set(mounted.map(item => item.id))], token).finally(following);
    for (const { entry, id } of mounted) {
      if (!result.ok) { entry.state = "failed"; entry.message = result.message; continue; }
      const outcome = result.data.results?.find(item => item.id === `mount-${id}`);
      entry.state = (result.data.launched || outcome?.dispatched) && outcome ? "sent" : "failed";
      entry.archive = outcome?.archive ?? result.data.archive ?? null;
      entry.message = outcome?.message ?? `The send to ${target.label} was not confirmed`;
    }
  } catch {
    fail(`The send to ${target.label} was interrupted. Check the receiving application before sending again.`);
  } finally {
    fail(`The send to ${target.label} was not confirmed`);
    activity.busy = false;
    remember(entries);
    publish();
    announce(target, entries);
    await onmounted(); // Timeline sends may create another recorded-orientation media dependency.
  }
}
