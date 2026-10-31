import { render, screen } from "@testing-library/svelte";
import { tick } from "svelte";
import { afterEach, expect, it, vi } from "vitest";
import EngineStatus from "../src/components/EngineStatus.svelte";
import { engine } from "../src/lib/events";

afterEach(() => {
  vi.useRealTimers();
  engine.set("reconnecting");
});

it("says nothing while the engine runs, or while the window first connects", async () => {
  vi.useFakeTimers();
  engine.set("reconnecting");
  render(EngineStatus);
  expect(screen.queryByRole("status")).toBeNull();
  engine.set("connected");
  await vi.advanceTimersByTimeAsync(5000);
  expect(screen.queryByRole("status")).toBeNull();
});

it("shows a lost connection after a moment and a stopped engine at once", async () => {
  vi.useFakeTimers();
  engine.set("connected");
  render(EngineStatus);
  engine.set("reconnecting");
  await tick();
  expect(screen.queryByRole("status")).toBeNull();
  await vi.advanceTimersByTimeAsync(2000);
  expect(screen.getByRole("status")).toHaveTextContent("Reconnecting to the engine");
  engine.set("stopped");
  await tick();
  expect(screen.getByRole("status")).toHaveTextContent("Engine stopped");
  expect(screen.getByRole("status")).toHaveTextContent("mcrawsome server --port");
  engine.set("connected");
  await tick();
  expect(screen.queryByRole("status")).toBeNull();
});
