import { expect, it } from "vitest";
import { proxyOrigin } from "../dev-origin";

it("rewrites only the running dev server's origins", () => {
  const own = ["http://localhost:5174/", "http://127.0.0.1:5174/"];
  expect(proxyOrigin("http://localhost:5174", own, "http://127.0.0.1:8642")).toBe("http://127.0.0.1:8642");
  expect(proxyOrigin("https://untrusted.example", own, "http://127.0.0.1:8642")).toBe("https://untrusted.example");
  expect(proxyOrigin(undefined, own, "http://127.0.0.1:8642")).toBeUndefined();
});
