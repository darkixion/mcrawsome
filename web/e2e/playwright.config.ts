import { defineConfig } from "@playwright/test";
import { homedir } from "node:os";
import { join } from "node:path";

export default defineConfig({
  testDir: ".",
  outputDir: join(homedir(), ".cache/mcrawsome-e2e/results"),
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 30_000 },
  reporter: "list",
  use: {
    browserName: "chromium",
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
