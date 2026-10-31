export type FileMenuStatus = {
  provider: string; label: string; supported: boolean; enabled: boolean; current: boolean;
  requires_admin: boolean; can_install_support: boolean; message: string;
};
export type FileMenuResult = { ok: boolean; status: FileMenuStatus; message?: string; cancelled?: boolean };
