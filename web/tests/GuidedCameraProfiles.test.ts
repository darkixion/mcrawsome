import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import CameraProfiles from "../src/components/GuidedCameraProfiles.svelte";
import CameraCalibrationGuide from "../src/components/CameraCalibrationGuide.svelte";
import { CAPTURE_STEPS, TARGET } from "../src/lib/camera-guide";

const profile = { name: "pixel_main", match: { device_model: "Pixel 8", width: 2048, height: 1536 }, focal_px: 1450 };
const measured = { profile, passed: true, issues: [], document: { name: "pixel_main", frame_readout_time: null,
  mcrawsome: { quality: { rms_px: .3, held_out_rms_px: .4, training_views: 30, held_out_views: 8, coverage_fraction: .8 } } } };
let passed = true;
let savedProfiles: { profile: typeof profile; user: boolean }[] = [];
let deleteFails = false;

beforeEach(() => {
  passed = true;
  savedProfiles = []; deleteFails = false;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/v1/camera-capture/analyse") return Response.json({ ...measured, passed, issues: passed ? [] : ["Capture more edge views"] });
    if (url === "/api/v1/camera-capture/recordings") return Response.json({ path: "/clips/linear.mp4", name: "linear.mp4", kind: "video", width: 2048, height: 1536, fps: 24, camera: "Pixel 8", gyro: false });
    if (url === "/api/v1/camera-capture" && init?.method === "POST") {
      savedProfiles = [{ profile, user: true }, ...savedProfiles];
      return Response.json({ profile, user: true });
    }
    if (url.startsWith("/api/v1/camera-capture/") && init?.method === "DELETE") {
      if (deleteFails) return Response.json({ error: { code: "output-unwritable", message: "Could not delete the camera profile" } }, { status: 422 });
      const name = decodeURIComponent(url.split("/").at(-1)!);
      savedProfiles = savedProfiles.filter(entry => !entry.user || entry.profile.name !== name);
      return Response.json({ profiles: savedProfiles });
    }
    if (url === "/api/v1/camera-capture") return Response.json({ profiles: savedProfiles });
    if (url === "/api/v1/library") return Response.json({ clips: [{ id: "board", name: "board.mcraw", camera: "Pixel 8", status: "ready", width: 2048, height: 1536 }], missing_folders: [] });
    if (url === "/api/v1/opened-clips") return Response.json({ clips: [], missing_folders: [] });
    return Response.json({}, { status: 404 });
  }));
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); delete window.pywebview; });

async function analyse(): Promise<void> {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Manage profiles" }));
  await screen.findByRole("option", { name: /board.mcraw/ });
  await user.selectOptions(screen.getByLabelText("Calibration recording"), "board");
  await user.click(screen.getByRole("button", { name: "Analyse and create JSON" }));
}

it("creates JSON, exposes independent validation, saves the actual result, then installs explicitly", async () => {
  const save = vi.fn(async () => true);
  window.pywebview = { api: { save_report: save, choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn() } };
  render(CameraProfiles);
  expect(fetch).not.toHaveBeenCalled();
  await analyse();
  expect(await screen.findByText("JSON profile ready")).toBeVisible();
  expect(screen.getByText("0.400 px")).toBeVisible();
  const analysis = vi.mocked(fetch).mock.calls.find(([url]) => url === "/api/v1/camera-capture/analyse");
  expect(JSON.parse(String(analysis?.[1]?.body))).toMatchObject({ clip_id: "board", model: "opencv_standard" });
  expect(vi.mocked(fetch).mock.calls.filter(([url, init]) => url === "/api/v1/camera-capture" && init?.method === "POST")).toHaveLength(0);
  await userEvent.click(screen.getByRole("button", { name: "Save camera profile JSON" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith(JSON.stringify(measured.document, null, 2) + "\n", "pixel_main.json"));
  await userEvent.click(screen.getByRole("button", { name: "Add profile to MCRAWsome" }));
  expect(await screen.findByRole("button", { name: "Camera profile added" })).toBeDisabled();
});

it("offers diagnostic JSON but prevents installing a capture that failed quality checks", async () => {
  passed = false;
  render(CameraProfiles);
  await analyse();
  expect(await screen.findByText("Capture needs improvement")).toBeVisible();
  expect(screen.getByText("Capture more edge views")).toBeVisible();
  expect(screen.getByRole("button", { name: "Add profile to MCRAWsome" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save camera profile JSON" })).toBeEnabled();
});

it("opens a linear calibration video directly and submits its path without importing it into the library", async () => {
  render(CameraProfiles);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Manage profiles" }));
  await user.type(screen.getByLabelText("Local calibration recording path"), "/clips/linear.mp4");
  await user.click(screen.getByRole("button", { name: "Open recording" }));
  await waitFor(() => expect(screen.getByLabelText("Calibration recording")).toHaveValue("file:/clips/linear.mp4"));
  await user.click(screen.getByRole("button", { name: "Analyse and create JSON" }));
  expect(await screen.findByText("JSON profile ready")).toBeVisible();
  const request = vi.mocked(fetch).mock.calls.find(([url]) => url === "/api/v1/camera-capture/analyse");
  expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ path: "/clips/linear.mp4", clip_id: null });
  expect(vi.mocked(fetch).mock.calls.some(([url, init]) => url === "/api/v1/opened-clips" && init?.method === "POST")).toBe(false);
});

it("renders the correct static target, counts down each step, pauses and gates the farther pass", async () => {
  vi.useFakeTimers();
  render(CameraCalibrationGuide, { onclose: vi.fn() });
  expect(TARGET).toMatchObject({ columns: 10, rows: 7, innerCorners: [9, 6] });
  const board = screen.getByRole("img", { name: /Calibration checkerboard/ });
  const geometry = board.innerHTML;
  expect(board.querySelectorAll('rect[fill="black"]')).toHaveLength(35);
  await fireEvent.click(screen.getByRole("button", { name: "Start countdown" }));
  await vi.advanceTimersByTimeAsync(1000);
  expect(screen.getByRole("timer")).toHaveTextContent("11");
  await fireEvent.click(screen.getByRole("button", { name: "Pause" }));
  await vi.advanceTimersByTimeAsync(5000);
  expect(screen.getByRole("timer")).toHaveTextContent("11");
  await fireEvent.click(screen.getByRole("button", { name: "Resume" }));
  const beforeGate = CAPTURE_STEPS.slice(0, CAPTURE_STEPS.findIndex(step => step.gate));
  for (let i = 0; i < beforeGate.length; i++) await vi.advanceTimersByTimeAsync((beforeGate[i]!.seconds - (i === 0 ? 1 : 0)) * 1000 + 100);
  expect(screen.getByRole("heading", { name: "Move farther away" })).toBeVisible();
  await vi.advanceTimersByTimeAsync(30000);
  expect(screen.getByRole("heading", { name: "Move farther away" })).toBeVisible();
  expect(board.innerHTML).toBe(geometry);
  await fireEvent.click(screen.getByRole("button", { name: "Start farther pass" }));
  await vi.advanceTimersByTimeAsync(100);
  expect(screen.getByRole("heading", { name: "Line up from farther away" })).toBeVisible();
});

it("pauses when display geometry changes", async () => {
  vi.useFakeTimers();
  render(CameraCalibrationGuide, { onclose: vi.fn() });
  await fireEvent.click(screen.getByRole("button", { name: "Start countdown" }));
  await fireEvent(window, new Event("resize"));
  expect(screen.getByText(/Display size changed/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Resume" })).toBeVisible();
});


it("deletes a saved capture and lets its name be used for a replacement", async () => {
  render(CameraProfiles);
  await analyse();
  await userEvent.click(screen.getByRole("button", { name: "Add profile to MCRAWsome" }));
  await userEvent.click(await screen.findByRole("button", { name: "Delete profile pixel_main" }));
  await userEvent.click(screen.getByRole("button", { name: /^Delete profile$/ }));
  expect(await screen.findByText(/Deleted camera profile pixel_main/)).toBeVisible();
  expect(screen.queryByRole("region", { name: "Calibration result" })).not.toBeInTheDocument();
  expect(screen.queryByText("Installed profiles (1)")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Analyse and create JSON" }));
  await userEvent.click(await screen.findByRole("button", { name: "Add profile to MCRAWsome" }));
  expect(await screen.findByRole("button", { name: "Camera profile added" })).toBeDisabled();
});

it("lets deletion be cancelled and keeps built-in profiles read-only", async () => {
  savedProfiles = [{ profile, user: true }, { profile: { ...profile, name: "builtin" }, user: false }];
  render(CameraProfiles);
  await userEvent.click(screen.getByRole("button", { name: "Manage profiles" }));
  await userEvent.click(await screen.findByRole("button", { name: "Delete profile pixel_main" }));
  await userEvent.click(screen.getByRole("button", { name: /^Cancel$/ }));
  expect(screen.queryByRole("button", { name: "Delete profile builtin" })).not.toBeInTheDocument();
  expect(screen.getByText("Installed profiles (2)")).toBeVisible();
  expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
});

it("retains the profile and reports a failed deletion", async () => {
  savedProfiles = [{ profile, user: true }]; deleteFails = true;
  render(CameraProfiles);
  await userEvent.click(screen.getByRole("button", { name: "Manage profiles" }));
  await userEvent.click(await screen.findByRole("button", { name: "Delete profile pixel_main" }));
  await userEvent.click(screen.getByRole("button", { name: /^Delete profile$/ }));
  expect(await screen.findByText("Could not delete the camera profile")).toBeVisible();
  expect(screen.getByText("Installed profiles (1)")).toBeVisible();
});
