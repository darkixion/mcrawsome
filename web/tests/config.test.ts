// @vitest-environment node
/// <reference types="node" />
import { expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

it("type-checks the build and test configuration", () => {
  const compiler = fileURLToPath(new URL("../node_modules/@typescript/native/bin/tsc", import.meta.url));
  const diagnostics = execFileSync(process.execPath, [compiler, "-p", "tsconfig.node.json", "--pretty", "false"], { encoding: "utf8" });
  expect(diagnostics).toBe("");
});
