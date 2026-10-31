import { test as base, expect, type Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createWriteStream } from "node:fs";
import { copyFile, mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
export const clipName = "clip_10bit_rotated.mcraw";
export type Engine = { url: string; folder: string; sidecar: string };

async function freePort(): Promise<number> {
  const socket = createServer();
  socket.listen(0, "127.0.0.1");
  await once(socket, "listening");
  const address = socket.address();
  if (!address || typeof address === "string") throw new Error("No test port");
  await new Promise<void>((resolve, reject) => socket.close(error => error ? reject(error) : resolve()));
  return address.port;
}

// The settings sections start open in every test, as tests of the controls assume; folding has tests of its own.
const OPEN_SECTIONS = { source: true, stabilise: true, resolve: true, review: true, "raw-media": true, "raw-advanced": true };

export const test = base.extend<{ engine: Engine }>({
  page: async ({ page }, use) => {
    await page.addInitScript(open => {
      try { if (localStorage.getItem("mcrawsome.open-sections") === null) localStorage.setItem("mcrawsome.open-sections", JSON.stringify(open)); }
      catch { /* storage refused: sections keep their defaults */ }
    }, OPEN_SECTIONS);
    await use(page);
  },
  engine: async ({}, use, info) => {
    const cache = process.env.MCRAWSOME_E2E_CACHE ?? join(homedir(), ".cache/mcrawsome-e2e");
    await mkdir(cache, { recursive: true });
    const scratch = await mkdtemp(join(cache, "engine-"));
    const folder = join(scratch, "clips");
    await mkdir(folder);
    for (const name of [clipName, "clip_10bit_rotated.gcsv"]) {
      await copyFile(join(root, "tests/fixtures/data", name), join(folder, name));
    }
    const port = await freePort();
    const url = `http://127.0.0.1:${port}`;
    const logPath = join(scratch, "engine.log");
    const log = createWriteStream(logPath);
    const python = process.env.MCRAWSOME_E2E_PYTHON;
    const command = python ?? "uv";
    const prefix = python ? ["-m", "mcrawsome.cli.main"] : ["run", "--offline", "mcrawsome"];
    const child = spawn(command, [...prefix, "server", "--host", "127.0.0.1", "--port", String(port), "--workers", "2", "--library", scratch], {
      cwd: root,
      detached: true,
      env: {
        ...process.env,
        XDG_CONFIG_HOME: join(scratch, "config"),
        XDG_CACHE_HOME: join(scratch, "cache"),
        XDG_STATE_HOME: join(scratch, "state"),
        XDG_DATA_HOME: join(scratch, "data"),
        MCRAWSOME_CACHE_DIR: join(scratch, "cache/mcrawsome"),
        MCRAWSOME_STATE_DIR: join(scratch, "state/mcrawsome"),
        MCRAWSOME_MOUNT_DIR: join(scratch, "mounts"),
        MCRAWSOME_SERVER_TOKEN: "",
        MCRAWSOME_NO_AUTOSTART: "1",
        TMPDIR: scratch,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.stdout.pipe(log, { end: false });
    child.stderr.pipe(log, { end: false });
    let launchError: Error | undefined;
    child.on("error", error => { launchError = error; });
    try {
      await expect.poll(async () => {
        if (launchError) throw launchError;
        if (child.exitCode !== null) throw new Error(await readFile(logPath, "utf8"));
        try { return (await fetch(`${url}/api/v1/health`, { signal: AbortSignal.timeout(1000) })).ok; }
        catch { return false; }
      }, { message: `Engine did not start; see ${logPath}` }).toBe(true);
      await use({ url, folder, sidecar: join(folder, "clip_10bit_rotated.mcrawsome.toml") });
    } finally {
      if (child.pid) {
        const exited = child.exitCode !== null || child.signalCode !== null ? Promise.resolve() : once(child, "close");
        const timeout = new AbortController();
        try {
          try { process.kill(-child.pid, "SIGTERM"); } catch { /* already stopped */ }
          await Promise.race([exited, delay(15_000, undefined, { signal: timeout.signal })]);
          try { process.kill(-child.pid, "SIGKILL"); } catch { /* already stopped */ }
          await exited;
        } finally { timeout.abort(); }
      }
      await new Promise<void>(resolve => log.end(resolve));
      if (info.status !== info.expectedStatus) {
        await info.attach("engine.log", { path: logPath, contentType: "text/plain" });
        console.log(`Engine files retained at ${scratch}`);
      } else await rm(scratch, { recursive: true, force: true });
    }
  },
});
export { expect };

export async function addFolder(page: Page, engine: Engine): Promise<void> {
  await page.goto(`${engine.url}/#/settings`);
  await page.getByRole("textbox", { name: "Add a folder" }).fill(engine.folder);
  const saved = page.waitForResponse(response => response.url().endsWith("/settings") && response.request().method() === "PUT");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  expect((await saved).ok()).toBe(true);
  await page.getByRole("link", { name: "Library", exact: true }).click();
  await expect(page.getByRole("option", { name: clipName, exact: true })).toBeEnabled();
  // A connected engine shows no status: only a stopped or lost engine does.
  await expect(page.getByText("Engine stopped", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Reconnecting to the engine", { exact: true })).toHaveCount(0);
}

/** Open a folding section of the panel under the preview, unless it is open already. */
export async function openSection(page: Page, name: RegExp): Promise<void> {
  const fold = page.getByRole("button", { name });
  if (await fold.getAttribute("aria-expanded") === "false") await fold.click();
  await expect(fold).toHaveAttribute("aria-expanded", "true");
}

export async function openDevelop(page: Page, engine: Engine): Promise<void> {
  await addFolder(page, engine);
  await page.getByRole("option", { name: clipName, exact: true }).click();
  await page.getByRole("tab", { name: "Resolve Settings", exact: true }).click();
  await expect(page.getByRole("img", { name: "Clip preview", exact: true })).toBeVisible();
}

export async function markTrim(page: Page, engine: Engine): Promise<{ start: number; duration: number }> {
  const position = page.getByRole("slider", { name: "Position", exact: true });
  // The approved fixture has just three frames. Use about one second if it is ever lengthened.
  const details = await (await page.request.get(`${engine.url}/api/v1/library`)).json();
  const rate = details.clips[0].fps as number;
  const last = Number(await position.getAttribute("max"));
  expect(last).toBeGreaterThanOrEqual(2);
  const start = 1;
  const end = Math.min(last, start + Math.round(rate));
  // I and O mark at the playhead (the Out mark includes the frame shown), as dragging the handles would.
  await position.fill(String(start));
  await position.blur();
  await page.keyboard.press("i");
  await position.fill(String(end - 1));
  await position.blur();
  await page.keyboard.press("o");
  await expect(page.getByRole("status").filter({ hasText: /^Saved$/ })).toHaveText("Saved");
  const trim = { start: Number((start / rate).toFixed(6)), duration: Number(((end - start) / rate).toFixed(6)) };
  const sidecar = await readFile(engine.sidecar, "utf8");
  expect(sidecar).toContain("[trim]");
  expect(sidecar).toContain(`start = "${trim.start}s"`);
  expect(sidecar).toContain(`duration = "${trim.duration}s"`);
  return trim;
}
