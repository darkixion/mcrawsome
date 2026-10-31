import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import { PRODUCT } from "../src/lib/brand";
import About from "../src/screens/About.svelte";

afterEach(() => vi.unstubAllGlobals());

function engine(): ReturnType<typeof vi.fn> {
  const fetch = vi.fn(async (url: string) => {
    if (url === "/api/v1/about") {
      return new Response(JSON.stringify({
        version: "0.1.0",
        checks: [{ name: "FFmpeg", status: "ok", detail: "8.1", fix: null }],
        licences: ["Python.txt", "Svelte.txt"],
      }), { status: 200 });
    }
    const file = decodeURIComponent(url.split("/").pop() ?? "");
    return new Response(JSON.stringify({ file, text: `${file} licence text` }), { status: 200 });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

it("shows the product, version, doctor and every licence file", async () => {
  const fetch = engine();
  render(About);
  expect(await screen.findByText("0.1.0")).toBeInTheDocument();
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(`About ${PRODUCT}`);
  expect(screen.getByText(/independent open-source project for MCRAW recordings/)).toHaveTextContent("not affiliated with or endorsed by MotionCam");
  expect(screen.getByRole("heading", { name: "System check" })).toBeVisible();
  expect(screen.getByText("8.1")).toBeInTheDocument();
  expect(screen.getByText("Python.txt")).toBeInTheDocument();
  expect(screen.getByText("Svelte.txt")).toBeInTheDocument();
  expect(screen.getByText("/usr/share/doc")).toBeInTheDocument();
  // No licence text is fetched until it is opened.
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("fetches a licence's text when it is opened", async () => {
  const fetch = engine();
  render(About);
  const summary = await screen.findByText("Svelte.txt");
  const details = summary.closest("details") as HTMLDetailsElement;
  details.open = true;
  await fireEvent(details, new Event("toggle"));
  expect(await screen.findByText("Svelte.txt licence text")).toBeInTheDocument();
  expect(fetch).toHaveBeenCalledWith("/api/v1/about/licences/Svelte.txt", expect.anything());
  expect(screen.queryByText("Python.txt licence text")).not.toBeInTheDocument();
});
