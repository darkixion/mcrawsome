interface Window {
  pywebview?: {
    api: {
      preview_available?: import("./native-preview").NativeApi["preview_available"];
      preview_present?: import("./native-preview").NativeApi["preview_present"];
      preview_bounds?: import("./native-preview").NativeApi["preview_bounds"];
      preview_overlay?: import("./native-preview").NativeApi["preview_overlay"];
      preview_crop?: import("./native-preview").NativeApi["preview_crop"];
      preview_hide?: import("./native-preview").NativeApi["preview_hide"];
      request_fullscreen?: (elementId: string) => Promise<boolean>;
      media_available?: () => Promise<boolean>;
      file_menu_status?: () => Promise<import("./file-manager-menu").FileMenuStatus>;
      set_file_menu?: (enabled: boolean) => Promise<import("./file-manager-menu").FileMenuResult>;
      file_association_status?: () => Promise<{ supported: boolean; enabled: boolean }>;
      set_file_association?: (enabled: boolean) => Promise<{ supported: boolean; enabled: boolean }>;
      file_association?: () => Promise<boolean>;
      associate_files?: () => Promise<boolean>;
      choose_session?: (save: boolean) => Promise<string | null>;
      choose_files?: () => Promise<string[]>;
      choose_folder: () => Promise<string | null>;
      load_vignette_profile?: () => Promise<unknown | null>;
      save_vignette_profile?: (profile: import("./api").Schemas["VignetteProfile"], name: string) => Promise<boolean>;
      save_capture_csv?: (id: string, name: string) => Promise<boolean>;
      save_calibration_target?: () => Promise<boolean>;
      save_camera_profile?: (name: string) => Promise<boolean>;
      save_report?: (content: string, name: string) => Promise<boolean>;
      save_text?: (content: string, name: string) => Promise<boolean>;
      open_folder: (path: string) => Promise<unknown>;
      reveal_file?: (path: string) => Promise<void>;
      notify?: (summary: string, body: string) => Promise<boolean>;
      restart_engine: () => Promise<void>;
    };
  };
}
