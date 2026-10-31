import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import GyroData from "../src/components/GyroData.svelte";
import { gyroPaths } from "../src/lib/gyro-plots";

afterEach(() => vi.unstubAllGlobals());
const bucket = { start_s: 0, end_s: 1, minimum: [-2, 0, 0], maximum: [6, 1, 2], mean: [1, 0.5, 1] };
const report = { samples: 400, duration_s: 1, sample_rate_hz: 400, angular_velocity: [bucket], acceleration: [bucket] };

it("reads gyro on expansion and lets the user switch axes and acceleration", async () => {
  const fetch = vi.fn(async () => Response.json(report));
  vi.stubGlobal("fetch", fetch);
  render(GyroData, { id: "a" });
  expect(fetch).not.toHaveBeenCalled();
  const details = screen.getByText("Gyro data").closest("details")!;
  details.open = true;
  await fireEvent(details, new Event("toggle"));
  const svg = await screen.findByRole("img", { name: "Recorded gyro angular velocity" });
  expect(svg.querySelectorAll("path[stroke-width]")).toHaveLength(3);
  expect(fetch).toHaveBeenCalledWith("/api/v1/clips/a/gyro", expect.objectContaining({ signal: expect.any(AbortSignal) }));
  const user = userEvent.setup();
  await user.click(screen.getByRole("switch", { name: "X" }));
  expect(svg.querySelectorAll("path[stroke-width]")).toHaveLength(2);
  await user.click(screen.getByRole("switch", { name: "Show acceleration" }));
  expect(screen.getByRole("img", { name: "Recorded acceleration" })).toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("reports unreadable logs without showing a misleading empty plot", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: { code: "input-unreadable", message: "Unreadable gyro samples" } }, { status: 422 })));
  render(GyroData, { id: "a" });
  const details = screen.getByText("Gyro data").closest("details")!;
  details.open = true;
  await fireEvent(details, new Event("toggle"));
  await waitFor(() => expect(screen.getByText("Unreadable gyro samples")).toBeVisible());
  expect(screen.queryByRole("img")).toBeNull();
});

it("uses extrema for a shared symmetric scale even when the mean hides a spike", () => {
  const plot = gyroPaths([bucket], 1);
  expect(plot.bound).toBe(6);
  expect(plot.series[0]!.envelope).not.toMatch(/NaN|Infinity/);
  expect(gyroPaths([], 0).series[0]!.mean).toBe("");
});
