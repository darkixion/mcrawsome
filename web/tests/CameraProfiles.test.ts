import { cleanup, render, screen, waitFor } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import CameraProfiles from "../src/components/CameraProfiles.svelte";

const profile = { name: "test_camera_lens", description: "Measured target", match: { device_model: "Test phone", camera_id: "0/2", width: 1280, height: 720, fps: 24 }, lens_calibration: { model: "opencv_standard", width: 640, height: 360, original_width: 1280, original_height: 720, camera_matrix: [[500,0,320],[0,500,180],[0,0,1]], coefficients: [0,0,0,0,0], rms_error: .3, image_count: 24 } };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("guides target capture and analyses before saving a phone/camera profile", async () => {
  let saved = false;
  const requests: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    requests.push(`${init?.method ?? "GET"} ${url}`);
    if (url === "/api/v1/camera-profiles" && init?.method === "POST") { saved=true; return Response.json({ path: "/config/cameras/test_camera_lens.toml", profile }); }
    if (url === "/api/v1/camera-profiles") return Response.json({ profiles: saved ? [{ profile, user: true }] : [] });
    if (url === "/api/v1/opened-clips") return Response.json({ clips: [{ id: "target", kind: "raw", status: "ready", name: "target.mcraw" }], missing_folders: [] });
    if (url === "/api/v1/camera-profiles/analyse") return Response.json({ profile, sampled_images: 60, detected_images: 24, notes: ["Lens calibration does not measure gyro timing."] });
    throw Error(String(url));
  }));
  render(CameraProfiles);
  const user=userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Manage camera profiles" }));
  await user.click(screen.getByRole("button", { name: "Add camera profile" }));
  expect(screen.getByRole("link", { name: "Download calibration target" })).toHaveAttribute("href", "/api/v1/camera-profiles/target.svg");
  expect(screen.getByText(/Move the phone while the target stays still/)).toBeVisible();
  await user.type(screen.getByRole("textbox", { name: "Target recording path" }), "/clips/target.mcraw");
  await user.click(screen.getByRole("button", { name: "Analyse recording" }));
  expect(await screen.findByText("Test phone · Camera 0/2")).toBeVisible();
  expect(saved).toBe(false);
  expect(requests.some(request => request.includes("opened-clips"))).toBe(false);
  expect(screen.getByText(/0.300 pixels/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Save camera profile" }));
  await waitFor(() => expect(saved).toBe(true));
  expect(requests.indexOf("POST /api/v1/camera-profiles/analyse")).toBeLessThan(requests.indexOf("POST /api/v1/camera-profiles"));
  expect(await screen.findByRole("link", { name: "Download test_camera_lens" })).toBeVisible();
});
