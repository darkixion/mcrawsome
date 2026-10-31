import { describe, expect, it } from "vitest";
import { formatClock, formatFps, formatFrameSize, plural } from "../src/lib/format";
import { parseRoute } from "../src/lib/route";

describe("format", () => {
  it("formats clip lengths as a clock", () => {
    expect(formatClock(18.2)).toBe("00:18");
    expect(formatClock(605)).toBe("10:05");
    expect(formatClock(3725)).toBe("1:02:05");
  });

  it("names the common frame sizes and rates", () => {
    expect(formatFrameSize(3840, 2160)).toBe("4K");
    expect(formatFrameSize(1920, 1080)).toBe("HD");
    expect(formatFrameSize(4080, 3072)).toBe("4080 x 3072");
    expect(formatFps(24)).toBe("24 fps");
    expect(formatFps(23.976)).toBe("23.98 fps");
  });

  it("counts with the right noun", () => {
    expect(plural(1, "job")).toBe("1 job");
    expect(plural(3, "job")).toBe("3 jobs");
  });
});

describe("routes", () => {
  it("reads the hash", () => {
    expect(parseRoute("")).toEqual({ screen: "library" });
    expect(parseRoute("#/queue")).toEqual({ screen: "library" });
    expect(parseRoute("#/develop/ab12")).toEqual({ screen: "library" });
    expect(parseRoute("#/nowhere")).toEqual({ screen: "library" });
  });
});
