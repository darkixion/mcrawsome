import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, expect, it } from "vitest";
import ResolutionWarning from "../src/components/ResolutionWarning.svelte";
import { settings } from "../src/lib/settings";

afterEach(() => settings.set(null));

it("warns above the free-edition limit and explains full-resolution media", async () => {
  render(ResolutionWarning, { width: 4080, height: 3072 });
  const warning = screen.getByRole("button", { name: "Resolve timeline resolution limit" });
  await fireEvent.focus(warning);
  expect(screen.getByRole("tooltip")).toHaveTextContent("3840 x 2160");
  expect(screen.getByRole("tooltip")).toHaveTextContent("Media Bin sends and the media inside timelines keep their full mounted resolution");
  await fireEvent.keyDown(warning, { key: "Escape" });
  expect(screen.queryByRole("tooltip")).toBeNull();
});

it("does not warn for Studio and responds to edition changes", async () => {
  // Supply the same server-default values used by Settings; Studio only changes this preference.
  const studio = { folders: [], workers: null, mount_folder: null, show_preview_stats: false,
    preview_gpu: true, quad_bayer_remosaic: true, preview_cache_mib: 256, keep_engine: false, remount_on_start: false,
    resolve_separate_audio: false, resolve_link_audio: true, resolve_keywords: false, timecode: "zero" as const, timecode_zone: "computer" as const,
    resolve_route: "auto" as const, resolve_full_resolution: true, problem: null };
  settings.set(studio);
  render(ResolutionWarning, { width: 4080, height: 3072 });
  expect(screen.queryByRole("button")).toBeNull();
  settings.set({ ...studio, resolve_full_resolution: false });
  expect(await screen.findByRole("button", { name: "Resolve timeline resolution limit" })).toBeVisible();
});

it.each([[3840, 2160, true], [1920, 1080, true], [4080, 3072, false]])(
  "does not warn for %s x %s with raw=%s", (width, height, raw) => {
    render(ResolutionWarning, { width, height, raw });
    expect(screen.queryByRole("button")).toBeNull();
  },
);
