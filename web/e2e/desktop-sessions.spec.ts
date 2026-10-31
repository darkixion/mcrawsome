import { test, expect, type Engine } from "./engine";
import type { Page } from "@playwright/test";

async function savedSession(page: Page, engine: Engine): Promise<string> {
  const path = `${engine.folder}/Shoot.mcrawsome-session.json`;
  expect((await page.request.post(`${engine.url}/api/v1/session/save`, { data: { path } })).ok()).toBe(true);
  expect((await page.request.post(`${engine.url}/api/v1/session/new`)).ok()).toBe(true);
  return path;
}

async function expectLoaded(page: Page, path: string): Promise<void> {
  await expect(page).toHaveURL(/#\/library$/);
  await page.locator("details.sessions > summary").click();
  await expect(page.locator("details.sessions strong")).toHaveText("Shoot");
  await expect(page.locator("details.sessions strong")).toHaveAttribute("title", path);
}

test("a session file opened in a running window loads in the session bar, from any screen", async ({ page, engine }) => {
  const path = await savedSession(page, engine);
  await page.goto(`${engine.url}/#/settings`);
  await page.evaluate(file => window.dispatchEvent(new CustomEvent("mcrawsome-open-files", { detail: [file] })), path);
  await expectLoaded(page, path);
});

test("a session a new window was handed loads in the session bar", async ({ page, engine }) => {
  const path = await savedSession(page, engine);
  await page.goto(`${engine.url}/?session=${encodeURIComponent(path)}#/library`);
  await expectLoaded(page, path);
  expect(new URL(page.url()).search).toBe("");
});
