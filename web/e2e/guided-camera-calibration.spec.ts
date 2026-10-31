import { test, expect } from "./engine";
import { execFileSync } from "node:child_process";
import { join } from "node:path";

test("screen target enters full screen, stays square and pauses when full screen ends", async ({ page, engine }, info) => {
  await page.goto(`${engine.url}/#/settings`);
  await page.getByRole("button", { name: "Manage profiles", exact: true }).click();
  await expect(page.getByText("Recommended recording settings", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Show calibration target", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Camera calibration target", exact: true });
  await expect(dialog).toBeVisible();
  const target = page.getByRole("img", { name: /Calibration checkerboard/ });
  await expect(target.locator('rect[fill="black"]')).toHaveCount(35);
  await page.getByRole("button", { name: "Use full screen", exact: true }).click();
  await expect.poll(() => page.evaluate(() => document.fullscreenElement?.id.startsWith("camera-target-"))).toBe(true);
  const geometry = await target.evaluate(element => {
    const rect = element.querySelector('rect[fill="black"]')!;
    const bounds = rect.getBoundingClientRect();
    return { width: bounds.width, height: bounds.height };
  });
  expect(geometry.width).toBeGreaterThan(20);
  expect(Math.abs(geometry.width - geometry.height)).toBeLessThan(.05);
  await page.getByRole("button", { name: "Start countdown", exact: true }).click();
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath("camera-target-fullscreen.png") });
  await page.evaluate(() => document.exitFullscreen());
  await expect(page.getByRole("button", { name: "Resume", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Close guide", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole("combobox", { name: "Calibration recording", exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath("camera-profile-settings.png") });
});

test("linear calibration video reaches board analysis without normal library import", async ({ page, engine }) => {
  const path = join(engine.folder, "linear calibration.mp4");
  execFileSync("ffmpeg", ["-v", "error", "-f", "lavfi", "-i", "color=white:size=320x180:rate=24",
    "-frames:v", "24", "-c:v", "mpeg4", "-color_trc", "linear", "-movflags", "+write_colr", path]);
  const opened: string[] = [];
  page.on("request", request => { if (request.method() === "POST" && request.url().endsWith("/opened-clips")) opened.push(request.url()); });
  await page.goto(`${engine.url}/#/settings`);
  await page.getByRole("button", { name: "Manage profiles", exact: true }).click();
  await page.getByLabel("Local calibration recording path").fill(path);
  const inspection = page.waitForResponse(response => response.url().endsWith("/camera-capture/recordings"));
  await page.getByRole("button", { name: "Open recording", exact: true }).click();
  expect((await inspection).status()).toBe(200);
  await expect(page.getByRole("combobox", { name: "Calibration recording", exact: true })).toHaveValue(`file:${path}`);
  await page.getByLabel("Camera model", { exact: true }).fill("Test camera");
  const analysis = page.waitForResponse(response => response.url().endsWith("/camera-capture/analyse"));
  await page.getByRole("button", { name: "Analyse and create JSON", exact: true }).click();
  expect((await analysis).status()).toBe(422);
  await expect(page.getByRole("alert")).toContainText("distinct sharp boards");
  expect(opened).toEqual([]);
});

test("saved camera profiles can be deleted and their names reused", async ({ page, engine }, info) => {
  const profile = {
    name: "discarded_capture", match: { device_model: "Pixel 8", width: 2048, height: 1536 }, focal_px: 1450,
    lens_calibration: { model: "opencv_standard", width: 2048, height: 1536, original_width: 2048, original_height: 1536,
      camera_matrix: [[1450, 0, 1024], [0, 1450, 768], [0, 0, 1]], coefficients: [-0.1, 0.02, 0, 0, 0], image_count: 30, rms_error: .3 },
  };
  expect((await page.request.post(`${engine.url}/api/v1/camera-capture`, { data: { profile } })).ok()).toBe(true);
  await page.goto(`${engine.url}/#/settings`);
  await page.getByRole("button", { name: "Manage profiles", exact: true }).click();
  await expect(page.getByText("Installed profiles (2)", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Delete profile discarded_capture", exact: true }).click();
  await page.screenshot({ path: info.outputPath("camera-profile-delete.png") });
  await page.getByRole("button", { name: "Delete profile", exact: true }).click();
  await expect(page.getByText(/Deleted camera profile discarded_capture/)).toBeVisible();
  await expect(page.getByText("Installed profiles (1)", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Delete profile pixel_10/ })).toHaveCount(0);
  expect((await page.request.post(`${engine.url}/api/v1/camera-capture`, { data: { profile } })).ok()).toBe(true);
  await page.reload();
  await page.getByRole("button", { name: "Manage profiles", exact: true }).click();
  await expect(page.getByRole("button", { name: "Delete profile discarded_capture", exact: true })).toBeVisible();
});
