import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { test, expect, addFolder, clipName } from "./engine";

test("logs production details beside the clip and restores them after reload", async ({ page, engine }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await addFolder(page, engine);
  await page.getByRole("option", { name: clipName, exact: true }).click();
  await page.getByRole("tab", { name: "Notes", exact: true }).click();
  const editor = page.getByRole("region", { name: "Clip details editor", exact: true });
  await expect(editor.getByRole("textbox", { name: "Title", exact: true })).toBeEnabled();
  await editor.getByRole("textbox", { name: "Title", exact: true }).fill("Bentley House exterior");
  await editor.getByRole("textbox", { name: "Description", exact: true }).fill("Arrival towards the gate");
  await editor.getByRole("textbox", { name: "Keywords", exact: true }).fill("exterior, arrival");
  await editor.getByText("Production details", { exact: true }).click();
  for (const [label, value] of [["Scene", "12B"], ["Shot", "C"], ["Take", "14A"], ["Location", "Bentley House Exterior"], ["Camera", "A"], ["Notes", "Keep the end. Clean sound."]] as const) {
    await editor.getByRole("textbox", { name: label, exact: true }).fill(value);
  }
  await editor.getByRole("combobox", { name: "Tag", exact: true }).selectOption("good-take");
  await expect(editor.getByRole("status", { name: "Clip details save status", exact: true })).toHaveText("Saved");
  const document = JSON.parse(await readFile(join(engine.folder, clipName.replace(/\.mcraw$/, ".meta")), "utf8"));
  expect(document.format).toBe("mcrawsome.clip-details");
  expect(document.details).toMatchObject({ title: "Bentley House exterior", scene: "12B", take: "14A", tag: "good-take", keywords: ["exterior", "arrival"] });
  await page.reload();
  await page.getByRole("tab", { name: "Notes", exact: true }).click();
  await expect(editor.getByRole("textbox", { name: "Title", exact: true })).toHaveValue("Bentley House exterior");
  await editor.getByText("Production details", { exact: true }).click();
  await expect(editor.getByRole("textbox", { name: "Location", exact: true })).toHaveValue("Bentley House Exterior");
  await expect(editor.getByRole("textbox", { name: "Take", exact: true })).toHaveValue("14A");
  await expect(editor.getByRole("combobox", { name: "Tag", exact: true })).toHaveValue("good-take");
  await editor.scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(engine.folder, "../clip-details.png") });
  await test.info().attach("Clip details editor", { path: join(engine.folder, "../clip-details.png"), contentType: "image/png" });
});
