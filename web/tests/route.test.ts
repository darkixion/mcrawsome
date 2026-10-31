import { get } from "svelte/store";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  window.location.hash = "";
  vi.resetModules();
});

describe("route", () => {
  it("reads each screen", async () => {
    const { parseRoute } = await import("../src/lib/route");
    expect(parseRoute("#/library")).toEqual({ screen: "library" });
    expect(parseRoute("#/settings")).toEqual({ screen: "settings" });
    expect(parseRoute("#/about")).toEqual({ screen: "about" });
  });

  it("goes to the Library for anything else, old Resolve Settings links included", async () => {
    const { parseRoute } = await import("../src/lib/route");
    for (const hash of ["", "#/queue", "#/export", "#/nowhere", "#/develop", "#/develop/a%20b", "#/develop/%E0%A4%A"]) {
      expect(parseRoute(hash)).toEqual({ screen: "library" });
    }
  });

  it("starts with such a hash in the address", async () => {
    window.location.hash = "#/develop/%";
    const { route } = await import("../src/lib/route");
    expect(get(route)).toEqual({ screen: "library" });
  });
});
