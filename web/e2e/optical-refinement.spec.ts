import { test, expect, addFolder, clipName } from "./engine";

test("image-assisted refinement defaults on and its override survives reload", async ({ page, engine }, info) => {
  await addFolder(page, engine);
  await page.getByRole("option", { name: clipName, exact: true }).click();
  await page.getByRole("tab", { name: "Resolve Settings", exact: true }).click();
  const toggle = page.getByRole("switch", { name: "Image-assisted refinement", exact: true });
  await expect(toggle).toBeChecked();
  await toggle.uncheck();
  await expect(page.getByRole("status").filter({ hasText: /^Saved$/ })).toBeVisible();
  await toggle.scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("image-assisted-refinement.png") });
  await page.reload();
  const card = page.getByRole("option", { name: clipName, exact: true });
  await expect(card).toBeVisible();
  if (await card.getAttribute("aria-selected") !== "true") await card.click();
  await page.getByRole("tab", { name: "Resolve Settings", exact: true }).click();
  await expect(page.getByRole("switch", { name: "Image-assisted refinement", exact: true })).not.toBeChecked();
  await page.getByRole("switch", { name: "Image-assisted refinement", exact: true }).check();
  await expect(page.getByRole("status").filter({ hasText: /^Saved$/ })).toBeVisible();
});
