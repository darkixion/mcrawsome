import { render, screen } from "@testing-library/svelte";
import { expect, it, vi } from "vitest";
import PreviewCanvas from "../src/components/PreviewCanvas.svelte";
import type { Painter } from "../src/components/ScrubCanvas.svelte";


it("clears the native-to-browser handoff canvas when the decoded resting image takes over", async () => {
  const context = vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D);
  const onpainter = vi.fn();
  const view = render(PreviewCanvas, { frame: "blob:previous", native: true, onpainter });
  const paint = onpainter.mock.calls[0]![0] as Painter;
  paint(new Image(), { kind: "refined", index: 8, width: 64, height: 32 });
  expect(screen.getByLabelText("Scrub preview").style.display).toBe("block");
  await view.rerender({ frame: "blob:decoded", native: false });
  expect(screen.getByLabelText("Scrub preview").style.display).toBe("none");
  expect(screen.getByAltText("Clip preview")).not.toHaveClass("covered");
  view.unmount(); context.mockRestore();
});
