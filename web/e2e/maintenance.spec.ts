import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { test, expect, openDevelop } from "./engine";

test("support diagnostics collect on demand and download a reviewable report", async ({ page, engine }) => {
  await page.goto(`${engine.url}/#/about`);
  await page.getByRole("button", { name: "Collect support report" }).click();
  await page.getByText("Review support report", { exact: true }).click();
  const report = JSON.parse(await page.getByRole("textbox", { name: "Support report", exact: true }).inputValue());
  expect(Object.keys(report.files)).toEqual(expect.arrayContaining(["doctor.json", "system.json"]));
  expect(JSON.parse(report.files["system.json"]).mcrawsome).toBe("1.0.1");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Save support report JSON" }).click();
  const saved = await download;
  expect(saved.suggestedFilename()).toBe("mcrawsome-support.json");
  expect(JSON.parse(await readFile((await saved.path())!, "utf8"))).toEqual(report);
});

test("application reset backs up defaults, survives reload and restores them", async ({ page, engine }) => {
  const initial = await (await page.request.get(`${engine.url}/api/v1/adjustment-defaults`)).json();
  await page.request.put(`${engine.url}/api/v1/adjustment-defaults`, { data: { version: initial.version, values: { develop: { black_level: 42 } } } });
  await page.goto(`${engine.url}/#/settings`);
  await page.getByRole("button", { name: "Show reset options" }).click();
  await page.getByRole("button", { name: "Reset defaults", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "A recoverable backup was saved" })).toBeVisible();
  expect((await (await page.request.get(`${engine.url}/api/v1/adjustment-defaults`)).json()).values).toEqual({});
  await page.reload();
  await page.getByRole("button", { name: "Show reset options" }).click();
  await page.getByRole("button", { name: "Restore previous defaults" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Previous settings restored" })).toBeVisible();
  expect((await (await page.request.get(`${engine.url}/api/v1/adjustment-defaults`)).json()).values.develop.black_level).toBe(42);
});

test("cache controls reclaim completed reports and preserve protected audio", async ({ page, engine }) => {
  const root = resolve(engine.folder, "../cache/mcrawsome");
  await mkdir(`${root}/analysis`, { recursive: true });
  await mkdir(`${root}/audio`, { recursive: true });
  await writeFile(`${root}/analysis/report.json`, '{"saved":true}');
  await writeFile(`${root}/audio/playing.wav`, "protected playback");
  await page.goto(`${engine.url}/#/settings`);
  await page.getByRole("button", { name: "Show cache usage" }).click();
  await page.getByRole("button", { name: "Clear analysis" }).click();
  await expect(page.getByRole("button", { name: "Clear analysis" })).toBeDisabled();
  expect(await readFile(`${root}/audio/playing.wav`, "utf8")).toBe("protected playback");
  await expect(page.getByRole("button", { name: "Clear audio" })).toHaveCount(0);
});

test("live preview cache clears without closing the viewer or changing RAW settings", async ({ page, engine }, info) => {
  // The viewer's cache panel is a debug display.
  await page.goto(`${engine.url}/#/settings`);
  const saved = page.waitForResponse(response => response.url().endsWith("/settings") && response.request().method() === "PUT");
  await page.getByRole("switch", { name: "Show preview performance statistics" }).check();
  expect((await saved).ok()).toBe(true);
  await openDevelop(page, engine);
  const library = await (await page.request.get(`${engine.url}/api/v1/library`)).json();
  const recipeUrl = `${engine.url}/api/v1/clips/${library.clips[0].id}/recipe`;
  const before = (await (await page.request.get(recipeUrl)).json()).layer.values;
  await page.getByText("Preview cache", { exact: true }).click();
  const panel = page.getByRole("region", { name: "Live preview cache" });
  const clear = panel.getByRole("button", { name: "Clear preview frames" });
  await expect(clear).toBeEnabled();
  const response = page.waitForResponse(r => r.url().endsWith("/cache/preview/clear"));
  await clear.click();
  const result = await (await response).json();
  expect(result.active).toBe(true);
  expect(result.bytes).toBe(0);
  await expect(panel.getByRole("status")).toContainText("Playback and the current picture are preserved");
  await expect(page.getByRole("img", { name: "Clip preview", exact: true })).toBeVisible();
  await page.getByRole("slider", { name: "Position", exact: true }).fill("1");
  await expect(page.locator('img[alt="Clip preview"][data-frame-index="1"]')).toBeVisible();
  await panel.getByRole("button", { name: "Refresh preview cache usage" }).click();
  await expect(clear).toBeEnabled();
  expect((await (await page.request.get(recipeUrl)).json()).layer.values).toEqual(before);
  await panel.screenshot({ path: info.outputPath("live-preview-cache.png") });
});
