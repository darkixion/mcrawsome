import { render, screen } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import ClipCard from "../src/components/ClipCard.svelte";
import { refreshThumbnail, thumbnailRevisions } from "../src/lib/library";

it("retries a failed thumbnail after saved source colour changes", async () => {
  const { fireEvent, waitFor } = await import("@testing-library/svelte");
  thumbnailRevisions.set({});
  const clip = { id: "colour", gps: false, rating: 0, details_text: "", display_rotation: 0 as const, path: "/clips/renamed.mp4", paths: [], name: "renamed.mp4", kind: "log" as const, gyro: false, status: "ready" as const };
  const view = render(ClipCard, { clip, selected: false, onchoose: vi.fn(), prefetch: false });
  const image = view.container.querySelector("img")!;
  await fireEvent.error(image);
  expect(view.container.querySelector("img")).toBeNull();
  refreshThumbnail("colour");
  await waitFor(() => expect(view.container.querySelector("img")?.getAttribute("src")).toBe("/api/v1/clips/colour/thumbnail?revision=1"));
  thumbnailRevisions.set({});
});

it("shows the whole frame, turned as saved by the engine, over a blurred copy that fills the card's side", () => {
  const clip = { id: "a", path: "/clips/a.mcraw", paths: ["/clips/a.mcraw"], name: "a.mcraw", kind: "raw" as const, gps: false, rating: 0, details_text: "", gyro: false, status: "ready" as const, width: 3840, height: 2160, display_rotation: 90 as const };
  const view = render(ClipCard, { clip, selected: false, onchoose: vi.fn(), prefetch: false });
  const [backdrop, frame] = [...view.container.querySelectorAll(".thumbnail img")];
  expect(frame).toHaveClass("frame");
  expect(backdrop).toHaveClass("backdrop");
  expect(backdrop).toHaveAttribute("aria-hidden", "true");
  expect(backdrop!.getAttribute("src")).toBe(frame!.getAttribute("src"));
});

it.each([[23.7916, "23.79"], [23.976, "23.98"], [24.002, "24"], [29.97, "29.97"]])(
  "labels a recording measured at %s fps with the rate its mount keeps, %s", (measured, shown) => {
  render(ClipCard, { clip: { id: "a", path: "/clips/a.mcraw", paths: ["/clips/a.mcraw"], name: "a.mcraw", kind: "raw", gps: false, rating: 0, details_text: "", gyro: false, display_rotation: 0, status: "ready", fps: 24, measured_fps: measured, frame_count: 72 }, selected: false, onchoose: vi.fn(), prefetch: false });
  expect(screen.getByText("FPS:").closest("p")).toHaveTextContent(`FPS: ${shown} | Frames: 72`);
});

it("groups the format line and labels the real rounded frame rate without redundant settings chips", () => {
  render(ClipCard, { clip: { id: "a", path: "/clips/a.mcraw", paths: ["/clips/a.mcraw"], name: "a.mcraw", kind: "raw", gps: false, rating: 0, details_text: "", gyro: false, display_rotation: 0, status: "ready", width: 3840, height: 2160, fps: 24, measured_fps: 24.002, frame_count: 72, missing_frames: 0, duplicated_frames: 0, badges: ["has-settings"] }, selected: false, onchoose: vi.fn(), prefetch: false });
  expect(screen.getByText("Clip settings")).toBeInTheDocument();
  expect(screen.queryByText("Has settings")).toBeNull();
  expect(screen.getByText("RAW").closest("p")).toHaveTextContent("RAW");
  expect(screen.getByText("4K")).toBeInTheDocument();
  const label = screen.getByText("FPS:");
  expect(label.tagName).toBe("STRONG");
  expect(label.closest("p")).toHaveTextContent("FPS: 24 | Frames: 72 | Dropped: 0 | Duplicated: 0");
  expect(screen.queryByText(/24\.002|24\.000|→/)).toBeNull();
  expect(screen.getByRole("checkbox", { name: "Select a.mcraw" }).closest(".thumbnail")).toBeInTheDocument();
});

it("flags a likely orientation problem on the clip without applying a rotation", async () => {
  const clip = { id: "a", path: "/clips/a.mcraw", paths: ["/clips/a.mcraw"], name: "a.mcraw", kind: "raw" as const, gps: false, rating: 0, details_text: "", gyro: false, display_rotation: 0 as const, status: "ready" as const,
    orientation_hint: { appears_wrong: true, suggested_rotation: 90, confidence: 1, sampled_seconds: 5, message: "Orientation appears wrong. Gravity suggests 90° clockwise." } };
  const view = render(ClipCard, { clip, selected: false, onchoose: vi.fn(), prefetch: false });
  expect(screen.getByText("Check orientation")).toBeVisible();
  expect(screen.getByTitle(/Gravity suggests/)).toBeInTheDocument();
  await view.rerender({ clip: { ...clip, orientation_hint: { ...clip.orientation_hint, appears_wrong: false } } });
  expect(screen.queryByText("Check orientation")).toBeNull();
});

it("keeps gyro, GPS (each with its icon and colour) and mount chips together without card send, mount or audio controls", async () => {
  const clip = { id: "a", path: "/clips/a.mcraw", paths: ["/clips/a.mcraw"], name: "a.mcraw", kind: "raw" as const, display_rotation: 0 as const, status: "ready" as const, gps: true, rating: 0, details_text: "", gyro: true };
  const view = render(ClipCard, { clip, selected: false, onchoose: vi.fn(), oncontext: vi.fn(), prefetch: false });
  const unmounted = screen.getByText("Unmounted");
  expect(unmounted).toHaveClass("badge", "danger");
  expect(unmounted.closest(".badges")).toContainElement(screen.getByText("GPS"));
  expect(unmounted.closest(".badges")).toContainElement(screen.getByText("Gyro"));
  expect(screen.getByText("Gyro")).toHaveClass("badge", "violet");
  expect(screen.getByText("GPS")).toHaveClass("badge", "sky");
  expect(screen.getByText("Gyro").querySelector("svg")).not.toBeNull();
  expect(screen.getByText("GPS").querySelector("svg")).not.toBeNull();
  expect(unmounted.closest(".mount")).not.toBeNull(); // the mount's state, at the end of the row
  expect(screen.queryByRole("button", { name: /^(Mount|Unmount|Send to|Choose send)/ })).toBeNull();
  expect(screen.queryByRole("switch")).toBeNull();
  const mounts = [{ id: "a", source: clip.path, path: "/mounts/a", active: true, serving: true, source_available: true, sent: true, retained: true, in_use: false, frames: 10, quad_bayer_remosaic: true, stamp: [1,2,3,4] as [number,number,number,number] }];
  await view.rerender({ mounts });
  expect(screen.getByText("Mounted")).toHaveClass("badge", "success");
  expect(screen.getByText("Mounted").closest(".badges")).toBe(screen.getByText("GPS").closest(".badges"));
  expect(screen.getByText("Mounted").parentElement).toHaveClass("mount");
  expect(screen.getByText("Idle").closest(".badges")).toContainElement(screen.getByText("Mounted"));
  expect(screen.queryByText("Unmounted")).toBeNull();
  await view.rerender({ mounts: mounts.map(mount => ({ ...mount, serving: false })) });
  expect(screen.getByText("Mount needs attention")).toHaveClass("warn");
  await view.rerender({ clip: { ...clip, gps: false, gyro: false }, mounts: [] });
  expect(screen.queryByText("GPS")).toBeNull();
  expect(screen.queryByText("Gyro")).toBeNull();
});

it("combines RAW frame timing and removes sensor levels from the card", () => {
  const clip = { id: "a", path: "/clips/a.mcraw", paths: [], name: "a.mcraw", kind: "raw" as const, display_rotation: 0 as const, status: "ready" as const, gps: false, rating: 0, details_text: "", gyro: false,
    fps: 24, frame_count: 512, missing_frames: 2, duplicated_frames: 3, sensor_bits: 12, white_level: 4095, black_levels: [64,64,64,64] as [number,number,number,number] };
  const view = render(ClipCard, { clip, selected: false, onchoose: vi.fn(), prefetch: false });
  expect(screen.getByText("FPS:").closest("p")).toHaveTextContent("FPS: 24 | Frames: 512 | Dropped: 2 | Duplicated: 3");
  expect(view.container.querySelectorAll(".technical")).toHaveLength(1);
  expect(screen.queryByText(/12-bit|WL:|BL:/)).toBeNull();
});

it("shows available LOG timing without RAW counters and preserves unknown facts", async () => {
  const clip = { id: "log", path: "/clips/log.mp4", paths: [], name: "log.mp4", kind: "log" as const, display_rotation: 0 as const, status: "ready" as const, gps: false, rating: 0, details_text: "", gyro: false,
    fps: 24, frame_count: 512 };
  const view = render(ClipCard, { clip, selected: false, onchoose: vi.fn(), prefetch: false });
  expect(screen.getByText("FPS:").closest("p")).toHaveTextContent("FPS: 24 | Frames: 512");
  expect(screen.queryByText(/Dropped:|Duplicated:|Unmounted/)).toBeNull();
  await view.rerender({ clip: { ...clip, fps: undefined } });
  expect(screen.getByText("Frames:").closest("p")).toHaveTextContent("Frames: 512");
  await view.rerender({ clip: { ...clip, kind: "raw", frame_count: undefined } });
  expect(screen.getByText("FPS:").closest("p")).toHaveTextContent("FPS: 24 | Dropped: unknown | Duplicated: unknown");
});

it("opens the same clip context action from the three-dot button without choosing or sending", async () => {
  const clip = { id: "a", path: "/clips/a.mcraw", paths: ["/clips/a.mcraw"], name: "a.mcraw", kind: "raw" as const, gps: false, rating: 0, details_text: "", gyro: true, status: "ready" as const, display_rotation: 90 as const };
  const onchoose = vi.fn(), oncontext = vi.fn();
  const view = render(ClipCard, { clip, selected: true, onchoose, oncontext, prefetch: false });
  const button = screen.getByRole("button", { name: "More actions for a.mcraw" });
  expect(button).toHaveAttribute("aria-haspopup", "menu");
  await userEvent.setup().click(button);
  expect(oncontext).toHaveBeenCalledOnce();
  expect(onchoose).not.toHaveBeenCalled();
  await view.rerender({ menuOpen: true });
  expect(button).toHaveAttribute("aria-expanded", "true");
});

it("sets and clears stars on the card without selecting it", async () => {
  const { fireEvent, within } = await import("@testing-library/svelte");
  const clip = { id: "rating", path: "/clips/rating.mcraw", paths: [], name: "rating.mcraw", kind: "raw" as const, gps: false, rating: 3, details_text: "", gyro: false, status: "ready" as const, display_rotation: 0 as const };
  const onchoose = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => Response.json({ values: init?.body ? JSON.parse(String(init.body)).values : { rating: 3 }, version: "v", document: {}, storage: "sidecar", message: "Saved" })));
  try {
    render(ClipCard, { clip, selected: false, onchoose, prefetch: false });
    const stars = screen.getByRole("group", { name: "Rating for rating.mcraw" });
    expect(within(stars).getByRole("button", { name: "3 stars" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.setup().click(within(stars).getByRole("button", { name: "5 stars" }));
    await screen.findByRole("button", { name: "5 stars", pressed: true });
    expect(onchoose).not.toHaveBeenCalled();
    await fireEvent.keyDown(screen.getByRole("option", { name: "rating.mcraw" }), { key: "0" });
    await screen.findByRole("button", { name: "Unrated", pressed: true });
    const { annotationsFor } = await import("../src/lib/annotations");
    await annotationsFor("rating").flush();
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls.at(-1)?.[1]?.body)).values.rating).toBe(0);
  } finally { vi.unstubAllGlobals(); }
});

it("replaces Unmounted with a yellow Mounting pill, then Mounted", async () => {
  const clip = { id: "a", path: "/clips/a.mcraw", paths: [], name: "a.mcraw", kind: "raw" as const, display_rotation: 0 as const, status: "ready" as const, gps: false, rating: 0, details_text: "", gyro: false, badges: ["recovered" as const] };
  const view = render(ClipCard, { clip, selected: false, onchoose: vi.fn(), prefetch: false, mounting: true });
  expect(screen.getByText("Mounting")).toHaveClass("badge", "warn");
  expect(screen.getByText("Recovered")).toHaveClass("warn");
  expect(screen.queryByText("Unmounted")).toBeNull();
  expect(screen.queryByText("Mounted")).toBeNull();
  await view.rerender({ mounting: false, mounts: [{ id: "m", source: clip.path, path: "/mounts/a", active: true, serving: true, source_available: true, sent: false, retained: false, frames: 3, quad_bayer_remosaic: true, stamp: [1,2,3,4] }] });
  expect(screen.getByText("Mounted")).toHaveClass("success");
  expect(screen.queryByText("Mounting")).toBeNull();
});


it("puts the timeline warning next to an oversized resolution without selecting the clip", async () => {
  const choose = vi.fn();
  render(ClipCard, { clip: { id: "large", path: "/clips/large.mcraw", paths: ["/clips/large.mcraw"], name: "large.mcraw", kind: "raw", gps: false, rating: 0, details_text: "", gyro: false, display_rotation: 0, status: "ready", width: 4080, height: 3072 }, selected: true, onchoose: choose, prefetch: false });
  const warning = screen.getByRole("button", { name: "Resolve timeline resolution limit" });
  expect(warning.closest("p")).toHaveClass("format");
  await userEvent.click(warning);
  expect(screen.getByRole("tooltip")).toHaveTextContent("3840 x 2160");
  expect(choose).not.toHaveBeenCalled();
});

it("skims through the clip under the pointer, showing the time there, and returns to its thumbnail", async () => {
  const { fireEvent, waitFor } = await import("@testing-library/svelte");
  const { forgetSkims } = await import("../src/lib/skims");
  const frames = Array.from({ length: 10 }, (_, index) => `frame${index}`);
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ frames })));
  const clip = { id: "s", path: "/clips/s.mcraw", paths: ["/clips/s.mcraw"], name: "s.mcraw", kind: "raw" as const, gps: false, rating: 0, details_text: "", gyro: false, status: "ready" as const, display_rotation: 0 as const, duration_s: 100 };
  const view = render(ClipCard, { clip, selected: false, onchoose: vi.fn(), prefetch: false });
  const thumbnail = view.container.querySelector<HTMLElement>(".thumbnail")!;
  thumbnail.getBoundingClientRect = () => ({ left: 0, width: 200, top: 0, height: 100, right: 200, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
  const frame = () => view.container.querySelector(".thumbnail img.frame")!.getAttribute("src");
  await fireEvent.pointerMove(thumbnail, { clientX: 70 });
  await waitFor(() => expect(frame()).toBe("data:image/jpeg;base64,frame3"), { timeout: 2000 });
  expect(view.container.querySelector(".duration")).toHaveTextContent("00:35");
  expect(vi.mocked(fetch).mock.calls.map(([url]) => String(url))).toEqual(["/api/v1/clips/s/skim"]);
  await fireEvent.pointerMove(thumbnail, { clientX: 199 });
  expect(frame()).toBe("data:image/jpeg;base64,frame9");
  await fireEvent.pointerLeave(thumbnail);
  expect(frame()).toBe("/api/v1/clips/s/thumbnail");
  expect(view.container.querySelector(".duration")).toHaveTextContent("01:40");
  forgetSkims();
  vi.unstubAllGlobals();
});
