import { test, expect, addFolder, clipName } from "./engine";

test("keeps card timing together and actions reachable through its menu and inspector", async ({ page, engine }) => {
  await addFolder(page, engine);
  const card = page.getByRole("option", { name: clipName, exact: true });
  await expect(card.locator(".technical")).toHaveCount(1);
  await expect(card.locator(".technical")).toHaveText("FPS: 24 | Frames: 3 | Dropped: 0 | Duplicated: 0");
  await expect(card.getByText(/-bit|WL:|BL:/)).toHaveCount(0);
  await expect(card.getByRole("button", { name: /^(Mount|Unmount|Send to|Choose send)/ })).toHaveCount(0);
  await expect(card.getByRole("switch")).toHaveCount(0);
  await card.getByRole("button", { name: `More actions for ${clipName}` }).click();
  const menu = page.getByRole("menu", { name: "Clip actions" });
  await expect(menu.getByRole("menuitem", { name: "Mount", exact: true })).toBeEnabled();
  await expect(menu.getByRole("menuitem", { name: "Send to Resolve (Media Bin)", exact: true })).toBeEnabled();
  await page.keyboard.press("Escape");
  await card.click();
  const inspector = page.getByRole("complementary", { name: "Clip workspace" });
  await expect(inspector.getByRole("button", { name: "Send to Resolve (Media Bin)", exact: true })).toHaveCount(0);
  await inspector.getByRole("tab", { name: "Media", exact: true }).click();
  await expect(inspector.getByRole("button", { name: "Mount with current settings", exact: true })).toBeEnabled();
});

test("gives the thumbnail the card's whole left side and keeps the chips and stars with the details, within narrow cards", async ({ page, engine }) => {
  await addFolder(page, engine);
  const card = page.getByRole("option", { name: clipName, exact: true });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await card.scrollIntoViewIfNeeded();
    const gyro = await card.getByText("Gyro", { exact: true }).boundingBox();
    const mount = await card.getByText("Unmounted", { exact: true }).boundingBox();
    expect(gyro).not.toBeNull(); expect(mount).not.toBeNull();
    expect(mount!.y).toBeCloseTo(gyro!.y, 1);
    expect(mount!.height).toBeCloseTo(gyro!.height, 1);
    const box = (await card.boundingBox())!;
    const thumbnail = (await card.locator(".thumbnail").boundingBox())!;
    // Flush with the card's left, top and bottom edges, inside its 1 px border.
    expect(thumbnail.x - box.x).toBeLessThanOrEqual(1.5);
    expect(thumbnail.y - box.y).toBeLessThanOrEqual(1.5);
    expect(box.y + box.height - (thumbnail.y + thumbnail.height)).toBeLessThanOrEqual(1.5);
    const stars = (await card.getByRole("group", { name: `Rating for ${clipName}` }).boundingBox())!;
    const camera = (await card.getByText("Google Pixel 10 Pro XL", { exact: true }).boundingBox())!;
    expect(stars.x).toBeGreaterThan(thumbnail.x + thumbnail.width);  // with the details, not under the thumbnail
    expect(gyro!.x).toBeGreaterThan(thumbnail.x + thumbnail.width);
    if (width === 1440) expect(Math.abs(stars.y + stars.height / 2 - (camera.y + camera.height / 2))).toBeLessThan(stars.height / 2);
    expect(stars.y + stars.height).toBeLessThan(gyro!.y);  // above the chips
    expect(await card.locator(".body").evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  }
});

test("keeps the thumbnail duration readable with a light palette", async ({ page, engine }) => {
  await addFolder(page, engine);
  await page.addStyleTag({ content: ":root { --text: #192c36; }" });
  const card = page.getByRole("option", { name: clipName, exact: true });
  await expect(card.locator(".duration")).toHaveCSS("color", "rgb(229, 238, 239)");
});

test("draws a chip's icon in the chip's own text colour, not the card's grey", async ({ page, engine }) => {
  await addFolder(page, engine);
  const chip = page.getByRole("option", { name: clipName, exact: true }).getByText("Gyro", { exact: true });
  const ink = await chip.evaluate(element => getComputedStyle(element).color);
  await expect(chip.locator("svg")).toHaveCSS("color", ink);
  await expect(chip.locator("svg")).toHaveCSS("width", "12px");
});
