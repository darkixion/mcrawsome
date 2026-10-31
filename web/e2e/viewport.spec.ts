import { test, expect, openDevelop } from "./engine";

test("fits the whole picture inside the height limit and keeps controls in fullscreen", async ({ page, engine }) => {
  await page.setViewportSize({ width: 1600, height: 800 });
  await openDevelop(page, engine);
  const viewport = page.locator('[aria-label="Video viewport"]');
  const bounds = () => viewport.evaluate(element => ({
    picture: element.querySelector(".picture")!.getBoundingClientRect().height,
    image: element.querySelector(".image-surface")!.getBoundingClientRect().height,
  }));
  const regular = await bounds();
  expect(regular.image).toBeLessThanOrEqual(regular.picture + 1);
  await viewport.getByRole("button", { name: "Enter fullscreen", exact: true }).click();
  await expect.poll(() => page.evaluate(() => document.fullscreenElement?.getAttribute("aria-label"))).toBe("Video viewport");
  const full = await bounds();
  expect(full.image).toBeLessThanOrEqual(full.picture + 1);
  await expect(viewport.getByRole("slider", { name: "Position", exact: true })).toBeVisible();
  await expect(viewport.getByRole("slider", { name: "Volume", exact: true })).toBeVisible();
  await viewport.getByRole("button", { name: "Exit fullscreen", exact: true }).click();
  await expect.poll(() => page.evaluate(() => document.fullscreenElement)).toBeNull();
});

test("keeps the video visible while scrolling the expanded RAW controls", async ({ page, engine }) => {
  await page.setViewportSize({ width: 1600, height: 800 });
  await openDevelop(page, engine);
  await page.getByRole("button", { name: "Use inherited shading" }).scrollIntoViewIfNeeded();
  const viewport = page.getByLabel("Video viewport", { exact: true });
  const top = await viewport.evaluate(element => element.getBoundingClientRect().top);
  expect(top).toBeGreaterThanOrEqual(0);
  const play = await viewport.getByRole("button", { name: "Play", exact: true }).boundingBox();
  expect(play!.y + play!.height).toBeLessThanOrEqual(800);
});

test("Library fullscreen keeps transport controls and exits with Escape", async ({ page, engine }) => {
  const { addFolder, clipName } = await import("./engine");
  await addFolder(page, engine);
  await page.getByRole("option", { name: clipName, exact: true }).click();
  const viewport = page.getByLabel("Video viewport", { exact: true });
  await viewport.getByRole("button", { name: "Enter fullscreen", exact: true }).click();
  await expect.poll(() => page.evaluate(() => document.fullscreenElement?.getAttribute("data-mcrawsome-viewport"))).toBe("true");
  for (const name of ["Position", "Volume"]) await expect(viewport.getByRole("slider", { name, exact: true })).toBeVisible();
  await expect(viewport.getByRole("button", { name: "Exit fullscreen", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect.poll(() => page.evaluate(() => document.fullscreenElement)).toBeNull();
  await expect(viewport.getByRole("button", { name: "Enter fullscreen", exact: true })).toBeVisible();
  await expect(page.getByText("Could not change fullscreen mode", { exact: true })).toHaveCount(0);
});
