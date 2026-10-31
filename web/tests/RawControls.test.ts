import { fireEvent, render, screen } from "@testing-library/svelte";
import { beforeEach, expect, it, vi } from "vitest";
import RawControls from "../src/components/RawControls.svelte";
import { openSections } from "../src/lib/sections";

beforeEach(() => openSections.set({ source: true, stabilise: true, resolve: true, review: true, "raw-media": true, "raw-advanced": true }));

function renderRaw(props: import("svelte").ComponentProps<typeof RawControls>) {
  return render(RawControls, props); // with every section open (beforeEach)
}

it("shows recorded levels and writes four black sites in spatial Bayer order", async () => {
  const onchange = vi.fn();
  renderRaw({ recipe: {}, values: {}, blacks: [64, 65, 66, 67], white: 4095, onchange, onclear: vi.fn() });
  expect(screen.getByText("Recorded: 64, 65, 66, 67")).toBeInTheDocument();
  expect(screen.getByRole("spinbutton", { name: "Black top left" })).toHaveValue(64);
  await fireEvent.change(screen.getByRole("spinbutton", { name: "Black bottom right" }), { target: { value: "68.5" } });
  expect(onchange).toHaveBeenCalledWith("develop", "black_level", [64, 65, 66, 68.5]);
  await fireEvent.change(screen.getByRole("spinbutton", { name: "White level" }), { target: { value: "4000" } });
  expect(onchange).toHaveBeenLastCalledWith("develop", "white_level", 4000);
});

it("distinguishes inherited overrides and clears only this clip's selected calibration", async () => {
  const onclear = vi.fn(), onchange = vi.fn();
  renderRaw({ recipe: { develop: { black_level: 42, white_level: 3000, lens_shading: 0.5 } }, values: { develop: { white_level: 3000, lens_shading: 0.5 } }, blacks: [64, 65, 66, 67], white: 4095, onchange, onclear });
  expect(screen.getByRole("spinbutton", { name: "Black top left" })).toHaveValue(42);
  expect(screen.getByText("Inherited override")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Use inherited black levels" })).toBeDisabled();
  await fireEvent.click(screen.getByRole("button", { name: "Use inherited white level" }));
  expect(onclear).toHaveBeenLastCalledWith(["white_level", "level_policy"]);
  await fireEvent.click(screen.getByRole("button", { name: "Use inherited shading" }));
  expect(onclear).toHaveBeenLastCalledWith(["lens_shading", "lens_shading_mode"]);
  await fireEvent.change(screen.getByRole("combobox", { name: "Shading correction" }), { target: { value: "colour" } });
  expect(onchange).toHaveBeenLastCalledWith("develop", "lens_shading_mode", "colour");
});

it("does not submit empty or invalid numeric levels", async () => {
  const onchange = vi.fn();
  renderRaw({ recipe: {}, values: {}, blacks: [64, 65, 66, 67], white: 4095, onchange, onclear: vi.fn() });
  const white = screen.getByRole("spinbutton", { name: "White level" });
  for (const value of ["", "0", "64", "65536"]) await fireEvent.change(white, { target: { value } });
  const black = screen.getByRole("spinbutton", { name: "Black top left" });
  for (const value of ["", "-1", "4095", "65536"]) await fireEvent.change(black, { target: { value } });
  expect(onchange).not.toHaveBeenCalled();
});

it("chooses whether virtual CinemaDNG carries the lens shading as gain maps or baked into the values", async () => {
  const onchange = vi.fn(), onclear = vi.fn();
  const { rerender } = renderRaw({ recipe: {}, values: {}, blacks: [64, 65, 66, 67], white: 4095, onchange, onclear });
  const select = screen.getByRole("switch", { name: "Bake in Vignette Correction" });
  expect(select).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Use inherited mount shading" })).toBeDisabled();
  await fireEvent.click(select);
  expect(onchange).toHaveBeenLastCalledWith("develop", "mount_shading", "baked");
  await rerender({ recipe: { develop: { mount_shading: "baked" } }, values: { develop: { mount_shading: "baked" } }, blacks: [64, 65, 66, 67], white: 4095, onchange, onclear });
  expect(screen.getByRole("switch", { name: "Bake in Vignette Correction" })).toBeChecked();
  await fireEvent.click(screen.getByRole("button", { name: "Use inherited mount shading" }));
  expect(onclear).toHaveBeenLastCalledWith(["mount_shading"]);
});

it("chooses a constant frame rate for virtual CinemaDNG", async () => {
  const onchange = vi.fn();
  renderRaw({ recipe: {}, values: {}, blacks: [64, 65, 66, 67], white: 4095, onchange, onclear: vi.fn() });
  const rate = screen.getByRole("combobox", { name: "Mounted frame rate" });
  expect(rate).toHaveValue("keep");
  await fireEvent.change(rate, { target: { value: "drop-frame" } });
  expect(onchange).toHaveBeenLastCalledWith("develop", "mount_frame_rate", "drop-frame");
  await fireEvent.change(rate, { target: { value: "29.97" } });
  expect(onchange).toHaveBeenLastCalledWith("develop", "mount_frame_rate", 29.97);
});

it("separates recorded, static, named and inherited levels with atomic patches", async () => {
  const onpatch = vi.fn(), onclear = vi.fn();
  const props = { recipe: { develop: { black_level: 42, white_level: 3000 } }, values: {}, blacks: [64,65,66,67], white: 4095, onchange: vi.fn(), onclear, onpatch };
  const { rerender } = render(RawControls, props);
  const preset = screen.getByRole("combobox", { name: "White / Black Levels" });
  expect(preset).toHaveValue("inherited");
  await fireEvent.change(preset, { target: { value: "dynamic" } });
  expect(onpatch).toHaveBeenLastCalledWith({ level_policy: "recorded" });
  await rerender({ ...props, recipe: { develop: { ...props.recipe.develop, level_policy: "recorded" } }, values: { develop: { level_policy: "recorded" } } });
  expect(preset).toHaveValue("dynamic");
  await fireEvent.change(preset, { target: { value: "static" } });
  expect(onpatch).toHaveBeenLastCalledWith({ level_policy: "static" });
  await fireEvent.change(preset, { target: { value: "16383/1024" } });
  expect(onpatch).toHaveBeenLastCalledWith({ level_policy: "explicit", white_level: 16383, black_level: 1024 });
  await fireEvent.change(preset, { target: { value: "inherited" } });
  expect(onclear).toHaveBeenLastCalledWith(["level_policy", "black_level", "white_level"]);
});

it("offers vignette-only shading, an automatic mount crop and a camera name for Resolve", async () => {
  const onchange = vi.fn(), onclear = vi.fn();
  renderRaw({ recipe: {}, values: {}, blacks: [64, 65, 66, 67], white: 4095, onchange, onclear });
  await fireEvent.change(screen.getByRole("combobox", { name: "Shading correction" }), { target: { value: "vignette" } });
  expect(onchange).toHaveBeenLastCalledWith("develop", "lens_shading_mode", "vignette");
  await fireEvent.change(screen.getByRole("combobox", { name: "Mounted DNG crop" }), { target: { value: "auto" } });
  expect(onchange).toHaveBeenLastCalledWith("develop", "mount_crop", "auto");
  const model = screen.getByRole("combobox", { name: "Camera named in mounted DNGs" });
  expect(model).toHaveValue("");
  await fireEvent.change(model, { target: { value: "Panasonic Varicam RAW" } });
  expect(onchange).toHaveBeenLastCalledWith("develop", "mount_camera_model", "Panasonic Varicam RAW");
  await fireEvent.change(model, { target: { value: "" } });
  expect(onclear).toHaveBeenCalledWith(["camera_model_source", "mount_camera_model"]);
  await fireEvent.click(screen.getByRole("switch", { name: "Add the clip's name to the camera model" }));
  expect(onchange).toHaveBeenLastCalledWith("develop", "mount_unique_model", true);
});

it("normalises the exposure of mounted DNGs from the recording or the picture", async () => {
  const onchange = vi.fn();
  const view = renderRaw({ recipe: {}, values: {}, blacks: [64, 65, 66, 67], white: 4095, onchange, onclear: vi.fn() });
  const mode = screen.getByRole("combobox", { name: "Normalise exposure" });
  expect(mode).toHaveValue("off");
  expect(screen.queryByRole("spinbutton", { name: "Smoothing window (seconds)" })).not.toBeInTheDocument();
  await fireEvent.change(mode, { target: { value: "picture" } });
  expect(onchange).toHaveBeenLastCalledWith("develop", "mount_normalise_exposure", "picture");
  await view.rerender({ recipe: { develop: { mount_normalise_exposure: "picture" } }, values: {}, blacks: [64, 65, 66, 67], white: 4095, onchange, onclear: vi.fn() });
  const window = screen.getByRole("spinbutton", { name: "Smoothing window (seconds)" });
  expect(window).toHaveValue(1);
  await fireEvent.change(window, { target: { value: "2.5" } });
  expect(onchange).toHaveBeenLastCalledWith("develop", "mount_normalise_seconds", 2.5);
  await fireEvent.change(window, { target: { value: "0" } });
  expect(onchange).toHaveBeenCalledTimes(2);
});

it("reads a saved normalisation switch as the recorded mode", () => {
  renderRaw({ recipe: { develop: { mount_normalise_exposure: true } }, values: {}, blacks: [64, 65, 66, 67], white: 4095, onchange: vi.fn(), onclear: vi.fn() });
  expect(screen.getByRole("combobox", { name: "Normalise exposure" })).toHaveValue("recorded");
});

it("serves log-encoded mounted DNGs and offers the bits removed only for log", async () => {
  const onchange = vi.fn();
  const view = renderRaw({ recipe: {}, values: {}, blacks: [64, 65, 66, 67], white: 4095, onchange, onclear: vi.fn() });
  const curve = screen.getByRole("combobox", { name: "Transfer curve in mounted DNGs" });
  expect(curve).toHaveValue("linear");
  expect(screen.queryByRole("combobox", { name: "Bits removed" })).not.toBeInTheDocument();
  await fireEvent.change(curve, { target: { value: "log" } });
  expect(onchange).toHaveBeenLastCalledWith("develop", "mount_transfer", "log");
  await view.rerender({ recipe: { develop: { mount_transfer: "log" } }, values: { develop: { mount_transfer: "log" } }, blacks: [64, 65, 66, 67], white: 4095, onchange, onclear: vi.fn() });
  const bits = screen.getByRole("combobox", { name: "Bits removed" });
  expect(bits).toHaveValue("auto");
  await fireEvent.change(bits, { target: { value: "4" } });
  expect(onchange).toHaveBeenLastCalledWith("develop", "mount_reduce_bits", 4);
});

it("keeps custom inherited frame rates and camera names visible", () => {
  renderRaw({ recipe: { develop: { mount_frame_rate: 120, mount_camera_model: "Custom camera" } }, values: {}, blacks: [64, 64, 64, 64], white: 4095, onchange: vi.fn(), onclear: vi.fn() });
  expect(screen.getByRole("combobox", { name: "Mounted frame rate" })).toHaveValue("120");
  expect(screen.getByRole("combobox", { name: "Camera named in mounted DNGs" })).toHaveValue("Custom camera");
});

it("keeps Advanced RAW open through saved edits and says what RAW media is set to while folded", async () => {
  openSections.set({});
  const props = { recipe: {}, values: {}, blacks: [64,64,64,64], white: 4095, onchange: vi.fn(), onclear: vi.fn() };
  const view = render(RawControls, props);
  const media = screen.getByRole("button", { name: /^RAW media/ });
  expect(media).toHaveAttribute("aria-expanded", "false");
  expect(media).toHaveTextContent("Inherited levels · recorded rate");
  const advanced = screen.getByRole("button", { name: /^Advanced RAW/ });
  expect(screen.queryByRole("spinbutton", { name: "White level" })).toBeNull();
  await fireEvent.click(advanced);
  await view.rerender({ ...props, recipe: { develop: { white_level: 3500 } }, values: { develop: { white_level: 3500 } } });
  expect(advanced).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("spinbutton", { name: "White level" })).toHaveValue(3500);
});

it("places a custom crop through the clip screen's crop tools", async () => {
  const onchange = vi.fn();
  const croptools = { whole: { width: 3840, height: 2160 }, saved: null, block: 2, start: vi.fn(), set: vi.fn(), shape: vi.fn(), apply: vi.fn(), cancel: vi.fn() };
  const props = { recipe: {}, values: {}, blacks: [64,64,64,64], white: 4095, onchange, onclear: vi.fn(), croptools };
  const view = renderRaw(props);
  await fireEvent.change(screen.getByRole("combobox", { name: "Mounted DNG crop" }), { target: { value: "custom" } });
  expect(croptools.start).toHaveBeenCalledOnce();
  expect(onchange).not.toHaveBeenCalled();
  await view.rerender({ ...props, cropediting: { id: "a", box: { left: 0, top: 0, width: 3840, height: 2160 }, aspect: null } });
  await fireEvent.change(screen.getByRole("spinbutton", { name: "Crop width" }), { target: { value: "1920" } });
  expect(croptools.set).toHaveBeenLastCalledWith({ left: 0, top: 0, width: 1920, height: 2160 });
  await fireEvent.change(screen.getByRole("combobox", { name: "Shape" }), { target: { value: "recorded" } });
  expect(croptools.shape).toHaveBeenLastCalledWith(3840 / 2160);
  await fireEvent.click(screen.getByRole("button", { name: "Apply crop" }));
  expect(croptools.apply).toHaveBeenCalledOnce();
  await view.rerender({ ...props, cropediting: null, recipe: { develop: { mount_crop: "1920x1080+64+32" } }, croptools: { ...croptools, saved: { left: 64, top: 32, width: 1920, height: 1080 } } });
  expect(screen.getByText("1920 × 1080, 64 from the left and 32 from the top")).toBeInTheDocument();
  await fireEvent.click(screen.getByRole("button", { name: "Edit on preview" }));
  expect(croptools.start).toHaveBeenCalledTimes(2);
});

it("reads the colour filter differently and scales linear mounts only", async () => {
  const onchange = vi.fn(), onclear = vi.fn();
  const view = renderRaw({ recipe: { develop: { cfa_layout: "quad" } }, values: { develop: { cfa_layout: "quad" } }, blacks: [64, 64, 64, 64], white: 4095, onchange, onclear });
  expect(screen.getByRole("combobox", { name: "Layout" })).toHaveValue("quad");
  await fireEvent.change(screen.getByRole("combobox", { name: "Bayer order" }), { target: { value: "bggr" } });
  expect(onchange).toHaveBeenLastCalledWith("develop", "cfa_pattern", "bggr");
  await fireEvent.click(screen.getByRole("button", { name: "Use inherited colour filter" }));
  expect(onclear).toHaveBeenLastCalledWith(["cfa_layout", "cfa_pattern"]);
  await fireEvent.click(screen.getByRole("switch", { name: "Scale values to 16 bits" }));
  expect(onchange).toHaveBeenLastCalledWith("develop", "mount_scale", true);
  await view.rerender({ recipe: { develop: { mount_transfer: "log" } } });
  expect(screen.getByRole("switch", { name: "Scale values to 16 bits" })).toBeDisabled();
});

it("takes a colour matrix as nine numbers and clears it when emptied", async () => {
  const onchange = vi.fn(), onclear = vi.fn();
  renderRaw({ recipe: { camera: { colour_matrix: [1, 0, 0, 0, 1, 0, 0, 0, 1] } }, values: { camera: { colour_matrix: [1, 0, 0, 0, 1, 0, 0, 0, 1] } }, blacks: [64, 64, 64, 64], white: 4095, onchange, onclear });
  const input = screen.getByRole("textbox", { name: "XYZ to camera" });
  expect(input).toHaveValue("1, 0, 0, 0, 1, 0, 0, 0, 1");
  await fireEvent.change(input, { target: { value: "1 2 3" } });
  expect(screen.getByRole("alert")).toHaveTextContent("Enter nine numbers, row by row.");
  expect(onchange).not.toHaveBeenCalled();
  await fireEvent.change(input, { target: { value: "0.9, -0.2, -0.1, -0.4, 1.3, 0.1, -0.05, 0.15, 0.6" } });
  expect(onchange).toHaveBeenLastCalledWith("camera", "colour_matrix", [0.9, -0.2, -0.1, -0.4, 1.3, 0.1, -0.05, 0.15, 0.6]);
  await fireEvent.change(input, { target: { value: "" } });
  expect(onclear).toHaveBeenLastCalledWith(["camera.colour_matrix"]);
});
