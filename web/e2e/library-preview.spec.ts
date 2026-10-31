import { test, expect, addFolder, clipName } from "./engine";

test("the Library preview pane keeps a bounded width and its picture takes the video's shape", async ({ page, engine }, info) => {
  await addFolder(page, engine);
  for (const [width, pane] of [[1440, 560], [1000, 420]] as const) {  // 40em, or 30em at its narrowest
    await page.setViewportSize({ width, height: 900 });
    await page.getByRole("option", { name: clipName, exact: true }).click();
    const image = page.getByRole("img", { name: /Clip preview|Library thumbnail/ });
    await expect(image).toBeVisible();
    await expect.poll(async () => (await page.locator(".right-pane").boundingBox())?.width ?? 0).toBeCloseTo(pane, 0);
    const right = (await page.locator(".right-pane").boundingBox())!;
    const tools = (await page.locator(".library-pane > .tools").boundingBox())!;
    const selection = (await page.getByRole("toolbar", { name: "Selected clips" }).boundingBox())!;
    expect(right.y).toBeCloseTo(tools.y, 0);
    expect(tools.x + tools.width).toBeLessThan(right.x);
    expect(selection.x + selection.width).toBeLessThan(right.x);
    await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
    const picture = (await page.locator(".picture").boundingBox())!;
    const ratio = await image.evaluate((element: HTMLImageElement) => element.naturalWidth / element.naturalHeight);
    expect(Math.abs(picture.width / picture.height - ratio)).toBeLessThan(0.02);  // no bars beside the video
    if (width === 1440) await page.screenshot({ path: info.outputPath("library-raised-preview.png") });
    await page.getByRole("option", { name: clipName, exact: true }).click();  // deselect for the next size
  }
});
