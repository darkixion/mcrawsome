import FileAssociation from "../src/components/FileAssociation.svelte";
import { cleanup, render, screen, waitFor } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import FileManagerMenu from "../src/components/FileManagerMenu.svelte";
import type { FileMenuStatus } from "../src/lib/file-manager-menu";

const missing: FileMenuStatus = { provider: "nautilus", label: "GNOME Files (Nautilus)", supported: true, enabled: false, current: false, requires_admin: true, can_install_support: true, message: "Administrator access required" };
function desktop(status: FileMenuStatus, change: NonNullable<Window["pywebview"]>["api"]["set_file_menu"]) {
  window.pywebview = { api: { choose_folder: vi.fn(), open_folder: vi.fn(), restart_engine: vi.fn(), file_menu_status: vi.fn(async () => status), set_file_menu: change } };
}
afterEach(() => { cleanup(); delete window.pywebview; vi.restoreAllMocks(); });

it("states the administrator requirement and remains off when installation is declined", async () => {
  const change = vi.fn(async () => ({ ok: false, cancelled: true, status: missing }));
  desktop(missing, change);
  render(FileManagerMenu);
  expect(await screen.findByText(/Administrator access required/)).toBeVisible();
  const toggle = screen.getByRole("switch", { name: "MCRAWsome file menu" });
  await userEvent.click(toggle);
  expect(change).toHaveBeenCalledWith(true);
  await waitFor(() => expect(toggle).not.toBeChecked());
  expect(screen.queryByRole("alert")).toBeNull();
});

it("disables and re-enables the default menu", async () => {
  const available = { ...missing, enabled: true, current: true, requires_admin: false };
  const change = vi.fn(async (enabled: boolean) => ({ ok: true, status: { ...available, enabled, current: enabled } }));
  desktop(available, change);
  render(FileManagerMenu);
  const toggle = screen.getByRole("switch", { name: "MCRAWsome file menu" });
  await waitFor(() => expect(toggle).toBeEnabled());
  expect(toggle).toBeChecked();
  await userEvent.click(toggle);
  await waitFor(() => expect(toggle).not.toBeChecked());
  await userEvent.click(toggle);
  await waitFor(() => expect(toggle).toBeChecked());
  expect(change.mock.calls.map(([value]) => value)).toEqual([false, true]);
  expect(screen.queryByText(/Administrator access required/)).toBeNull();
});

it("waits for a late desktop bridge and can update an older provider's menu", async () => {
  render(FileManagerMenu);
  const toggle = screen.getByRole("switch", { name: "MCRAWsome file menu" });
  expect(toggle).toBeDisabled();
  const previous = { ...missing, enabled: true, requires_admin: false };
  const change = vi.fn(async () => ({ ok: true, status: { ...previous, current: true } }));
  desktop(previous, change);
  window.dispatchEvent(new Event("pywebviewready"));
  await userEvent.click(await screen.findByRole("button", { name: "Update file menu" }));
  await waitFor(() => expect(change).toHaveBeenCalledWith(true));
  expect(toggle).toBeChecked();
});

it("lets the user choose MCRAWsome for MCRAW files", async () => {
  desktop({ ...missing, enabled: true, current: true, requires_admin: false }, vi.fn());
  window.pywebview!.api.file_association = vi.fn(async () => false);
  const associate = vi.fn(async () => true);
  window.pywebview!.api.associate_files = associate;
  render(FileAssociation);
  const button = await screen.findByRole("button", { name: "Use MCRAWsome to open .mcraw files" });
  await userEvent.click(button);
  expect(associate).toHaveBeenCalledOnce();
  expect(await screen.findByText("MCRAWsome is the default application for .mcraw recordings.")).toBeVisible();
  expect(button).toBeDisabled();
});
