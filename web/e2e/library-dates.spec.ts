import { join } from "node:path";
import { test, expect, clipName } from "./engine";

test.use({ timezoneId: "Europe/London" });

test("quick date ranges combine with filters and survive a saved session", async ({ page, engine }, info) => {
  const opened = await page.request.post(`${engine.url}/api/v1/opened-clips`, { data: { paths: [join(engine.folder, clipName)] } });
  const [source] = (await opened.json()).clips;
  const clips = [
    { ...source, name: "today.mcraw", captured: "2026-10-25T10:00:00Z" },
    { ...source, id: `${source.id}-week`, name: "week.mcraw", captured: "2026-10-20T10:00:00Z" },
    { ...source, id: `${source.id}-month`, name: "month.mcraw", captured: "2026-10-01T10:00:00Z" },
  ];
  await page.request.put(`${engine.url}/api/v1/settings`, { data: { folders: [engine.folder] } });
  await page.route("**/api/v1/library", route => route.fulfill({ json: { clips, missing_folders: [] } }));
  await page.route("**/api/v1/events", route => route.fulfill({ contentType: "text/event-stream", body: ": test library snapshot\n\n" }));
  await page.clock.setFixedTime(new Date("2026-10-25T12:00:00Z"));
  await page.goto(`${engine.url}/#/library`);
  await page.getByRole("button", { name: "Library folders", exact: true }).click();
  const cards = page.getByRole("listbox", { name: "Clips", exact: true }).getByRole("option");
  await expect(cards).toHaveCount(3);
  await page.getByRole("button", { name: "Filter", exact: true }).click();
  const dates = page.getByRole("combobox", { name: "Filter Date", exact: true });
  await dates.selectOption({ label: "Today" });
  await expect(cards).toHaveCount(1);
  await expect(cards.first()).toHaveAttribute("aria-label", "today.mcraw");
  await dates.selectOption({ label: "This week" });
  await expect(cards).toHaveCount(2);
  await dates.selectOption({ label: "This month" });
  await expect(cards).toHaveCount(3);
  await dates.selectOption("2026-10-01");
  await expect(cards).toHaveCount(1);
  await expect(cards.first()).toHaveAttribute("aria-label", "month.mcraw");
  await dates.selectOption({ label: "This week" });
  await expect.poll(async () => (await (await page.request.get(`${engine.url}/api/v1/session`)).json()).workspace.facets.captured).toBe("@this-week");
  await page.locator("details.sessions > summary").click();
  const sessionPath = join(engine.folder, "dates.mcrawsome-session.json");
  await page.getByRole("textbox", { name: "Session file path", exact: true }).fill(sessionPath);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(`${engine.url}/api/v1/session`)).json()).path).toBe(sessionPath);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Open clips", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Filter (active)", exact: true }).click();
  await expect(dates).toHaveValue("@this-week");
  await page.screenshot({ path: info.outputPath("date-filters.png") });
});
