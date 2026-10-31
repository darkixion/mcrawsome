import { writeFile } from "node:fs/promises";
import { test, expect, openDevelop, markTrim } from "./engine";

test("review marker categories persist alongside RAW preparation controls", async ({ page, engine }, info) => {
  await openDevelop(page, engine);
  const review = page.getByRole("switch", { name: "Review markers", exact: true });
  await expect(review).toBeVisible();
  await expect(page.getByRole("switch", { name: "Motion markers", exact: true })).toHaveCount(0);
  await review.check();
  const categories = page.getByRole("group", { name: "Review marker categories", exact: true });
  await expect(categories.getByRole("switch")).toHaveCount(8);
  await categories.getByRole("switch", { name: "Highlight clipping", exact: true }).uncheck();
  const library = await (await page.request.get(`${engine.url}/api/v1/library`)).json();
  const recipeUrl = `${engine.url}/api/v1/clips/${library.clips[0].id}/recipe`;
  await expect.poll(async () => (await (await page.request.get(recipeUrl)).json()).layer.values.motion?.review_categories?.clipping).toBe(false);
  await page.reload();
  await expect(review).toBeChecked();
  await expect(categories.getByRole("switch", { name: "Highlight clipping", exact: true })).not.toBeChecked();
  await expect(page.getByRole("combobox", { name: "Sensor clipping", exact: true })).toBeVisible();
  await categories.locator("..").screenshot({ path: info.outputPath("review-marker-categories.png") });
});

test("RAW calibration saves per-clip overrides and restores folder values", async ({ page, engine }) => {
  await writeFile(`${engine.folder}/mcrawsome.toml`, "[develop]\nexposure = 0\nblack_level = 42\nwhite_level = 3000\nlens_shading = 0.7\nlens_shading_mode = \"colour\"\n");
  await openDevelop(page, engine);
  const library = await (await page.request.get(`${engine.url}/api/v1/library`)).json();
  const id = library.clips[0].id;
  const before = await (await page.request.get(`${engine.url}/api/v1/clips/${id}`)).json();
  const black = page.getByRole("spinbutton", { name: "Black top left", exact: true });
  const white = page.getByRole("spinbutton", { name: "White level", exact: true });
  await expect(black).toHaveValue("42");
  await black.fill("50.5"); await black.press("Tab");
  await white.fill("3500"); await white.press("Tab");
  await page.getByRole("combobox", { name: "Shading correction" }).selectOption("full");
  await page.getByRole("slider", { name: "Shading strength" }).fill("0.5");
  await expect(page.getByRole("status").filter({ hasText: /^Saved$/ })).toHaveText("Saved");
  const saved = await (await page.request.get(`${engine.url}/api/v1/clips/${id}/recipe`)).json();
  expect(saved.layer.values.develop).toEqual({ level_policy: "explicit", black_level: [50.5,42,42,42], white_level: 3500, lens_shading: 0.5, lens_shading_mode: "full" });
  expect(saved.inherited.develop.black_level).toBe(42);
  await page.reload();
  await expect(black).toHaveValue("50.5");
  await page.getByRole("button", { name: "Use inherited black levels" }).click();
  await page.getByRole("button", { name: "Use inherited white level" }).click();
  await page.getByRole("button", { name: "Use inherited shading" }).click();
  await expect(black).toHaveValue("42");
  await expect(white).toHaveValue("3000");
  await expect(page.getByRole("combobox", { name: "Shading correction" })).toHaveValue("colour");
  await expect(page.getByRole("slider", { name: "Shading strength" })).toHaveValue("0.7");
  await expect(page.getByRole("status").filter({ hasText: /^Saved$/ })).toHaveText("Saved");
  const reset = await (await page.request.get(`${engine.url}/api/v1/clips/${id}/recipe`)).json();
  expect(reset.layer.values).toEqual({});
  const camera = page.getByRole("combobox", { name: "Camera named in mounted DNGs" });
  await camera.selectOption("Panasonic Varicam RAW");
  await expect(page.getByRole("status", { name: "Resolve settings save status" })).toHaveText("Saved");
  await camera.selectOption("");
  await expect(page.getByRole("status", { name: "Resolve settings save status" })).toHaveText("Saved");
  expect((await (await page.request.get(`${engine.url}/api/v1/clips/${id}/recipe`)).json()).layer.values).toEqual({});
  const after = await (await page.request.get(`${engine.url}/api/v1/clips/${id}`)).json();
  expect(after.black_levels).toEqual(before.black_levels);
  expect(after.white_level).toEqual(before.white_level);
});


test("Resolve settings persist orientation, proxy, frame rate and trim", async ({ page, engine }) => {
  await openDevelop(page, engine);
  await page.getByRole("combobox", { name: "Orientation", exact: true }).selectOption("90");
  await page.getByRole("combobox", { name: "Proxy", exact: true }).selectOption("half");
  await page.getByRole("combobox", { name: "Mounted frame rate", exact: true }).selectOption("25");
  await expect(page.getByRole("status", { name: "Resolve settings save status" })).toHaveText("Saved");
  await markTrim(page, engine);
  await page.reload();
  await expect(page.getByRole("combobox", { name: "Orientation", exact: true })).toHaveValue("90");
  await expect(page.getByRole("combobox", { name: "Proxy", exact: true })).toHaveValue("half");
  await expect(page.getByRole("combobox", { name: "Mounted frame rate", exact: true })).toHaveValue("25");
  expect(await page.getByRole("combobox", { name: "Look", exact: true }).count()).toBe(0);
  expect(await page.getByRole("link", { name: "Export", exact: true }).count()).toBe(0);
});


test("recorded levels bypass fixed defaults, survive reload and reset to inherited", async ({ page, engine }) => {
  await writeFile(`${engine.folder}/mcrawsome.toml`, '[develop]\nblack_level = 42\nwhite_level = 3000\nmount_camera_model = "Panasonic Varicam RAW"\n');
  await openDevelop(page, engine);
  const levels = page.getByRole("combobox", { name: "White / Black Levels" });
  await expect(levels).toHaveValue("inherited");
  await levels.selectOption("dynamic");
  const saved = page.getByRole("status", { name: "Resolve settings save status" });
  await expect(saved).toHaveText("Saved");
  await page.reload();
  await expect(levels).toHaveValue("dynamic");
  const black = page.getByRole("spinbutton", { name: "Black top left", exact: true });
  await expect(black).not.toHaveValue("42");
  const camera = page.getByRole("combobox", { name: "Camera named in mounted DNGs" });
  await camera.selectOption("recorded");
  await expect(saved).toHaveText("Saved");
  await page.reload();
  await expect(camera).toHaveValue("recorded");
  await levels.selectOption("inherited");
  await camera.selectOption("");
  await expect(saved).toHaveText("Saved");
  await expect(black).toHaveValue("42");
  await expect(page.getByRole("spinbutton", { name: "White level", exact: true })).toHaveValue("3000");
});


test("sensor clipping is a paused preview diagnostic and does not save transforms", async ({ page, engine }) => {
  await openDevelop(page, engine);
  const library = await (await page.request.get(`${engine.url}/api/v1/library`)).json();
  const url = `${engine.url}/api/v1/clips/${library.clips[0].id}/recipe`;
  const before = (await (await page.request.get(url)).json()).layer;
  const view = page.getByRole("combobox", { name: "Sensor clipping", exact: true });
  await view.selectOption("any");
  await expect(page.getByRole("img", { name: "Pink sensor-clipping overlay" })).toBeVisible();
  await expect(page.getByText(/Pink marks recorded samples at or above sensor white/)).toBeVisible();
  await view.selectOption("all");
  await expect(page.getByRole("img", { name: "Pink sensor-clipping overlay" })).toBeVisible();
  await page.getByRole("combobox", { name: "Preview resolution", exact: true }).selectOption("source");
  await expect(page.getByRole("img", { name: "Pink sensor-clipping overlay" })).toBeVisible();
  await view.selectOption("off");
  await expect(page.getByRole("img", { name: "Pink sensor-clipping overlay" })).toHaveCount(0);
  expect((await (await page.request.get(url)).json()).layer).toEqual(before);
});


test("custom mounted crop persists and the preview adopts its geometry", async ({ page, engine }) => {
  await openDevelop(page, engine);
  await page.getByRole("combobox", { name: "Mounted DNG crop" }).selectOption("custom");
  await expect(page.getByRole("img", { name: /^Mounted DNG crop:/ })).toBeVisible();
  for (const [name, value] of [["Crop width", "1024"], ["Crop height", "1024"], ["Crop left", "64"], ["Crop top", "0"]]) {
    await page.getByRole("spinbutton", { name, exact: true }).fill(value);
    await page.getByRole("spinbutton", { name, exact: true }).press("Tab");
  }
  await page.getByRole("button", { name: "Apply crop", exact: true }).click();
  await expect(page.getByRole("status", { name: "Resolve settings save status" })).toHaveText("Saved");
  await expect(page.locator('[data-mcrawsome-viewport="true"]')).toHaveAttribute("style", /--aspect: 1024 \/ 1024/);
  await page.reload();
  await expect(page.getByText("1024 × 1024, 64 from the left and 0 from the top", { exact: true })).toBeVisible();
  await expect(page.locator('[data-mcrawsome-viewport="true"]')).toHaveAttribute("style", /--aspect: 1024 \/ 1024/);
});

test("folds the Resolve settings to a line each, opens Stabilise, and remembers what is opened", async ({ page, engine }) => {
  // From the sections' own defaults, not the open sections every other test starts with.
  await page.addInitScript(() => {
    if (sessionStorage.getItem("defaults") === null) { localStorage.removeItem("mcrawsome.open-sections"); sessionStorage.setItem("defaults", "1"); }
  });
  await openDevelop(page, engine);
  const settings = page.getByRole("complementary", { name: "Resolve settings", exact: true });
  await expect(settings.getByRole("button", { name: /^Stabilise(?!d)/ })).toHaveAttribute("aria-expanded", "true");
  const source = settings.getByRole("button", { name: /^Source/ });
  await expect(source).toHaveAttribute("aria-expanded", "false");
  await expect(source).toContainText("As recorded");
  await expect(settings.getByRole("button", { name: /^RAW media/ })).toContainText("recorded rate");
  await expect(page.getByRole("combobox", { name: "Orientation", exact: true })).toHaveCount(0);
  await source.click();
  await expect(page.getByRole("combobox", { name: "Orientation", exact: true })).toBeVisible();
  expect(JSON.parse(await page.evaluate(() => localStorage.getItem("mcrawsome.open-sections") ?? "{}"))).toEqual({ source: true });
  // Suggest trim sits with the settings' actions, not above the video.
  await expect(page.getByRole("region", { name: "Develop selected clips", exact: true }).getByRole("button", { name: "Suggest trim", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Suggest trim", exact: true })).toHaveCount(1);
});
