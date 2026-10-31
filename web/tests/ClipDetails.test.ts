import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import ClipDetails from "../src/components/ClipDetails.svelte";
import type { ClipSummary } from "../src/lib/library";

afterEach(() => vi.unstubAllGlobals());

// Capture checks load in the background beside the details; count only the details requests.
const detailRequests = () => vi.mocked(fetch).mock.calls.filter(([url]) => !String(url).endsWith("/checks"));

it("draws gain maps and noise curves only when Calibration is expanded", async () => {
  const clip = { id: "a", name: "a.mcraw", path: "/clips/a.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ...clip, metadata: { Container: {}, "First frame": { lensShadingMapWidth: 2, lensShadingMapHeight: 2, lensShadingMap: [[1,2,3,4],[1,2,3,4],[1,2,3,4],[1,2,3,4]], noiseProfile: [0.01,0.001,0.02,0.002,0.03,0.003,0.04,0.004] } } })));
  render(ClipDetails, { clip });
  const foldout = (await screen.findByText("Calibration")).closest("details")!;
  expect(screen.queryByRole("img", { name: /Lens shading/ })).toBeNull();
  foldout.open = true;
  await fireEvent(foldout, new Event("toggle"));
  expect(await screen.findAllByRole("img", { name: /Lens shading/ })).toHaveLength(4);
  expect(screen.getByRole("img", { name: "Recorded noise model" })).toBeInTheDocument();
  expect(screen.getByText(/Shared gain scale: 1.000/)).toBeInTheDocument();
  expect(screen.getAllByText("Noise Profile").length).toBeGreaterThan(0);
  expect(detailRequests()).toHaveLength(1);
});
it("keeps details visible during a same-clip refresh", async () => {
  const clip = { id: "a", name: "a.mcraw", path: "/clips/a.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  let finish: (value: Response) => void = () => {};
  let details = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (String(url).endsWith("/checks")) return Response.json({ checks: [] });
    details += 1;
    return details === 1 ? Response.json({ ...clip, width: 320, height: 180 }) : new Promise<Response>((resolve) => { finish = resolve; });
  }));
  const view = render(ClipDetails, { clip });
  await screen.findAllByText("320 x 180");
  await view.rerender({ clip: { ...clip, badges: ["has-settings"] } });
  await waitFor(() => expect(detailRequests()).toHaveLength(2));
  expect(screen.getAllByText("320 x 180")).toHaveLength(2);
  expect(screen.queryByText("Reading the clip...")).toBeNull();
  finish(Response.json({ ...clip, width: 640, height: 360 }));
  await screen.findAllByText("640 x 360");
});

it("asks for no new details when a Library refresh hands back an unchanged copy of the clip", async () => {
  const clip = { id: "a", name: "a.mcraw", path: "/clips/a.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  vi.stubGlobal("fetch", vi.fn(async (url: string) =>
    String(url).endsWith("/checks") ? Response.json({ checks: [] }) : Response.json({ ...clip, width: 320, height: 180 })));
  const view = render(ClipDetails, { clip });
  await screen.findAllByText("320 x 180");
  await view.rerender({ clip: { ...clip } });
  await screen.findAllByText("320 x 180");
  expect(detailRequests()).toHaveLength(1);
});

it("shows all capture facts and original metadata, including unknown nested fields", async () => {
  const clip = { id: "a", name: "a.mcraw", path: "/clips/a.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ...clip,
    width: 3840, height: 2160, fps: 24, measured_fps: 24.01, frame_count: 422,
    focal_length_mm: 25.4, aperture: 1.7, iso: 160, exposure_time_ns: 10000000,
    sensor_bits: 12, white_level: 4095, black_levels: [64,65,66,67], missing_frames: 2,
    gyro: false, dropped_frames: 2, filled_frames: 2,
    metadata: { Container: { manufacturer: "Example Camera", extraData: { futureField: "preserved value" } }, "First frame": { focusDistance: 1.25, lensShadingMap: [[1,1.1],[1.2,1.3]] } },
  })));
  render(ClipDetails, { clip });
  await screen.findByText("422");
  expect(screen.getByText("ISO")).toBeInTheDocument();
  expect(screen.getByText("160")).toBeInTheDocument();
  expect(screen.getByText("12-bit")).toBeInTheDocument();
  expect(screen.getByText("64, 65, 66, 67")).toBeInTheDocument();
  expect(screen.getByText("Example Camera")).toBeInTheDocument();
  expect(screen.getByText("preserved value")).toBeInTheDocument();
  expect(screen.getByText("1.25")).toBeInTheDocument();
});

it("organises metadata into readable categories and rounds capture summaries", async () => {
  const clip = { id: "a", name: "a.mcraw", path: "/clips/a.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ...clip, width: 3840, height: 2160, frame_count: 422, duration_s: 17.58, fps: 24, camera: "Example Camera", focal_length_mm: 25.76530647, aperture: 1.700000047, iso: 36, sensor_bits: 10, white_level: 1023, black_levels: [64,64,64,64], metadata: { Container: { extraData: { audioSampleRate: 48000, audioChannels: 2 }, colorMatrix1: [1,0,0,0,1,0,0,0,1] }, "First frame": {} } })));
  render(ClipDetails, { clip });
  await screen.findByRole("heading", { name: "Camera & lens" });
  expect(screen.getByRole("heading", { name: "Exposure" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "RAW data" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Audio" })).toBeInTheDocument();
  expect(screen.getByText("25.8 mm (35 mm equivalent)")).toBeInTheDocument();
  expect(screen.getByText("f/1.7")).toBeInTheDocument();
  expect(screen.getByText("48 kHz")).toBeInTheDocument();
  expect(screen.getByText("All recorded fields").closest("details")).not.toHaveAttribute("open");
});

it("places clip actions before information and locates the clicked Source path", async () => {
  const clip = { id: "a", name: "a.mcraw", path: "/clips/a #雪.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  const reveal = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("pywebview", { api: { reveal_file: reveal } });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(clip)));
  render(ClipDetails, { clip, raw: 1, onsend: vi.fn() });
  const source = await screen.findByTitle("Locate file");
  expect(source).toHaveTextContent(clip.path);
  const info = screen.getByText("CLIP INFORMATION");
  for (const label of ["Open Resolve Settings", "Send to Resolve (Media Bin)"]) {
    expect(screen.getByRole("button", { name: label }).compareDocumentPosition(info) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  }
  await fireEvent.click(source);
  expect(reveal).toHaveBeenCalledExactlyOnceWith(clip.path);
});

it("labels the calibration saved with each virtual media generation", async () => {
  const clip = { id: "a", name: "a.mcraw", path: "/clips/a.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(clip)));
  render(ClipDetails, { clip, mounts: [{ id: "mount", source: clip.path, path: "/mounts/a", quad_bayer_remosaic: true, stamp: [1,2,3,4], active: true, serving: true, frames: 3, source_available: true, sent: false, retained: false, raw_settings: { level_policy: "legacy" as const, camera_model_source: "override" as const, rotation: 0, black_level: [10,20,30,40], white_level: 3000, lens_shading: 0.5, lens_shading_mode: "colour", mount_shading: "opcode", mount_proxy: "full", mount_frame_rate: "keep", mount_crop: "none", mount_unique_model: false, mount_normalise_exposure: "off", mount_normalise_seconds: 1, mount_transfer: "linear", mount_reduce_bits: "auto", cfa_layout: "auto", cfa_pattern: "recorded", mount_scale: false, mount_first_frame: "proxy" } }] });
  expect(await screen.findByText("Saved black: 10, 20, 30, 40 · white: 3000")).toBeInTheDocument();
  expect(screen.getByText("Shading: 50% · colour only · as gain maps")).toBeInTheDocument();
});

it("says when a virtual media generation has its lens shading baked into the values", async () => {
  const clip = { id: "a", name: "a.mcraw", path: "/clips/a.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(clip)));
  render(ClipDetails, { clip, mounts: [{ id: "mount", source: clip.path, path: "/mounts/a", quad_bayer_remosaic: true, stamp: [1,2,3,4], active: true, serving: true, frames: 3, source_available: true, sent: false, retained: false, raw_settings: { level_policy: "legacy" as const, camera_model_source: "override" as const, rotation: 0, lens_shading: 1, lens_shading_mode: "full", mount_shading: "baked", mount_proxy: "half", mount_frame_rate: 25, mount_crop: "auto", mount_camera_model: "Panasonic Varicam RAW", mount_unique_model: false, mount_normalise_exposure: "recorded", mount_normalise_seconds: 1, mount_transfer: "linear", mount_reduce_bits: "auto", cfa_layout: "auto", cfa_pattern: "recorded", mount_scale: false, mount_first_frame: "proxy" } }] });
  expect(await screen.findByText("Shading: 100% · vignette and colour · baked into the values · half size proxy · 25 fps · exposure normalised · black edge removed · named Panasonic Varicam RAW")).toBeInTheDocument();
});

it("distinguishes timeline media and lets idle mounted media reconnect", async () => {
  const clip = { id: "a", name: "a.mcraw", path: "/clips/a.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(clip)));
  const mount = { id: "recorded", source: clip.path, path: "/mounts/a", quad_bayer_remosaic: true, stamp: [1,2,3,4] as [number,number,number,number], active: true, serving: true, frames: 3, source_available: true, in_use: false, sent: true, retained: true, raw_settings: { level_policy: "legacy" as const, camera_model_source: "override" as const, rotation: 0 as const, black_level: null, white_level: null, lens_shading: 1, lens_shading_mode: "full" as const, mount_shading: "opcode" as const, mount_proxy: "full" as const, mount_frame_rate: "keep" as const, mount_crop: "none" as const, mount_unique_model: false, mount_normalise_exposure: "off" as const, mount_normalise_seconds: 1, mount_transfer: "linear" as const, mount_reduce_bits: "auto" as const, cfa_layout: "auto" as const, cfa_pattern: "recorded" as const, mount_scale: false, mount_first_frame: "proxy" as const } };
  const action = vi.fn();
  const view = render(ClipDetails, { clip, mounts: [mount], onmountaction: action });
  expect(screen.getByText("Sent media · Recorded orientation")).toBeInTheDocument();
  await fireEvent.click(screen.getByRole("button", { name: "Remount" }));
  expect(action).toHaveBeenCalledExactlyOnceWith(mount, "remount");
  await view.rerender({ mounts: [{ ...mount, source_available: false }] });
  expect(screen.getByRole("button", { name: "Remount" })).toBeDisabled();
  expect(screen.getByText(/Source changed or is offline. Send the clip again/)).toBeInTheDocument();
});

it("shows valid embedded GPS with units while retaining the original fields", async () => {
  const clip = { id: "gps", name: "gps.mcraw", path: "/clips/gps.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ...clip, metadata: { Container: { extraData: { postProcessSettings: {
    gpsLatitude: 51.501, gpsLongitude: -0.12, gpsAltitude: -3.5, gpsTime: "2026-10-20T12:00:00Z",
  } } } } })));
  render(ClipDetails, { clip });
  const gps = await screen.findByRole("region", { name: "GPS" });
  expect(gps).toHaveTextContent("51.5010000°");
  expect(gps).toHaveTextContent("-0.1200000°");
  expect(gps).toHaveTextContent("-3.5 m");
  expect(gps).toHaveTextContent("2026-10-20T12:00:00Z");
  expect(gps.querySelector('[title*="north is positive"]')).not.toBeNull();
  expect(gps.querySelector('[title*="does not infer a timezone"]')).not.toBeNull();
});

it("omits the GPS summary for MotionCam's empty fix defaults", async () => {
  const clip = { id: "no-gps", name: "no-gps.mcraw", path: "/clips/no-gps.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ...clip, metadata: { Container: { extraData: { postProcessSettings: {
    gpsLatitude: 0, gpsLongitude: 0, gpsAltitude: 0, gpsTime: "",
  } } } } })));
  render(ClipDetails, { clip });
  await screen.findByRole("region", { name: "File" });
  expect(screen.queryByRole("region", { name: "GPS" })).toBeNull();
});

it("distinguishes the recorded frame from the larger sensor area", async () => {
  const clip = { id: "quad", name: "quad.mcraw", path: "/clips/quad.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ...clip, width: 3840, height: 2160,
    metadata: { Container: { sensorArrangment: "gbrg" }, "First frame": { width: 3840, height: 2160, originalWidth: 8160, originalHeight: 6144 } },
  })));
  render(ClipDetails, { clip, section: "info" });
  const recording = await screen.findByRole("region", { name: "Recording" });
  expect(recording).toHaveTextContent("Recorded frame");
  expect(recording).toHaveTextContent("3840 x 2160");
  const sensor = screen.getByRole("region", { name: "RAW data" });
  expect(sensor).toHaveTextContent("Sensor dimensions");
  expect(sensor).toHaveTextContent("8160 x 6144");
  expect(recording.querySelector('[title*="stored in the recording"]')).not.toBeNull();
});

it("shows LOG signal range and recorded telemetry without inventing RAW capture fields", async () => {
  const clip = { id: "log", name: "log.mp4", path: "/clips/log.mp4", kind: "log", status: "ready", badges: [] } as unknown as ClipSummary;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ...clip, sensor_bits: 10,
    camera: "Synthetic phone", focal_length_mm: 25, aperture: 1.8,
    metadata: { Video: { colour_range: "pc", variable_frame_rate: true, encoding: "davinci", colour_space: "DaVinci Wide Gamut", transfer_function: "DaVinci Intermediate", encoding_source: "Explicit profile" },
      Device: { EISA: "N" }, Telemetry: { gyro_samples: 2000, accelerometer: true, clock: "video", axes: "XYZ", start_s: 0.002, end_s: 5 } },
  })));
  render(ClipDetails, { clip, section: "info" });
  await screen.findByText("Video depth");
  expect(screen.getByText("Full")).toBeInTheDocument();
  const recording = screen.getByRole("region", { name: "Recording" });
  expect(recording).toHaveTextContent("Colour spaceDaVinci Wide Gamut");
  expect(recording).toHaveTextContent("Transfer functionDaVinci Intermediate");
  expect(recording).toHaveTextContent("Colour sourceExplicit profile");
  expect(screen.queryByText("davinci", { exact: true })).toBeNull();
  expect(screen.getByText("Variable (fixed-rate preview and export)")).toBeInTheDocument();
  expect(screen.getByText("2000")).toBeInTheDocument();
  expect(screen.getByText("Recorded")).toBeInTheDocument();
  expect(screen.getByText("0.002 to 5 s")).toBeInTheDocument();
  expect(screen.queryByText("ISO")).toBeNull();
  expect(screen.queryByText("Shutter")).toBeNull();
});

it("lists capture warnings the recording's data explains under Readiness", async () => {
  const clip = { id: "night", name: "night.mcraw", path: "/clips/night.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => String(url).endsWith("/checks")
    ? Response.json({ checks: [
      { name: "Frame rate", value: "Recorded at 23.79 fps rather than 24: the 1/24 s shutter fills the whole frame time", tone: "warn" },
      { name: "Sound channels", value: "Both channels are identical (mono sound)", tone: "plain" },
    ] })
    : Response.json({ ...clip, dropped_frames: 0, filled_frames: 0, gyro: true })));
  render(ClipDetails, { clip });
  expect(await screen.findByText(/23.79 fps rather than 24/)).toHaveClass("warn");
  expect(screen.getByText("Both channels are identical (mono sound)")).toHaveClass("plain");
});

it("reads capture warnings once per clip and cancels them when another clip is shown", async () => {
  const first = { id: "a", name: "a.mcraw", path: "/clips/a.mcraw", kind: "raw", status: "ready", badges: [] } as unknown as ClipSummary;
  const second = { ...first, id: "b", name: "b.mcraw", path: "/clips/b.mcraw" } as unknown as ClipSummary;
  const signals: AbortSignal[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (String(url).endsWith("/checks")) { signals.push(init!.signal!); return new Promise<Response>(() => {}); }
    return Response.json({ ...(String(url).includes("/b") ? second : first), dropped_frames: 0, filled_frames: 0 });
  }));
  const view = render(ClipDetails, { clip: first });
  await waitFor(() => expect(signals).toHaveLength(1));
  await view.rerender({ clip: { ...first, badges: ["has-settings"] } });
  await view.rerender({ clip: second });
  await waitFor(() => expect(signals).toHaveLength(2));
  expect(signals[0]!.aborted).toBe(true);
  expect(signals[1]!.aborted).toBe(false);
});
