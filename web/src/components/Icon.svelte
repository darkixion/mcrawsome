<script module lang="ts">
  // Line icons drawn for this interface (24-unit grid, stroked with the current colour).
  export type IconName = "library" | "develop" | "export" | "settings" | "about" | "start" | "back" | "play" | "pause" | "forward" | "end" | "fit" | "volume" | "muted" | "fullscreen" | "collapse" | "more" | "camera" | "film" | "clock" | "frames" | "levels" | "analytics" | "metadata" | "notes" | "folder" | "mount" | "unmount" | "import" | "reset" | "vignette" | "media"
    | "warning" | "zoom" | "overlay" | "off" | "gyro" | "acceleration" | "balance" | "noise" | "shutter" | "focus" | "black" | "white" | "replay" | "filter" | "trash" | "dock" | "undock" | "location" | "chevron";
</script>
<script lang="ts">
  let { name }: { name: IconName } = $props();
</script>

<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor"
  stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  {#if name === "start" || name === "back"}
    <path d="M18 5 8 12l10 7Z" />{#if name === "start"}<path d="M5 5v14" />{:else}<path d="M5 8v8" />{/if}
  {:else if name === "end" || name === "forward"}
    <path d="m6 5 10 7-10 7Z" />{#if name === "end"}<path d="M19 5v14" />{:else}<path d="M19 8v8" />{/if}
  {:else if name === "play"}<path d="m8 5 11 7-11 7Z" />
  {:else if name === "pause"}<path d="M8 5v14M16 5v14" />
  {:else if name === "volume" || name === "muted"}
    <path d="M3 9h4l5-4v14l-5-4H3Z" />
    {#if name === "muted"}<path d="m16 9 6 6m0-6-6 6" />{:else}<path d="M16 8a6 6 0 0 1 0 8M19 5a10 10 0 0 1 0 14" />{/if}
  {:else if name === "fit"}<rect x="3" y="5" width="18" height="14" rx="1" /><path d="m7 9 3 3-3 3m10-6-3 3 3 3" />
  {:else if name === "fullscreen"}<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5" />
  {:else if name === "collapse"}<path d="M3 8h5V3m8 0v5h5M8 21v-5H3m18 0h-5v5" />
  {:else if name === "dock"}<rect x="3" y="4" width="18" height="16" rx="1.5" /><path d="M15 4v16M12 12H6m3-3-3 3 3 3" />
  {:else if name === "undock"}<rect x="3" y="4" width="18" height="16" rx="1.5" /><path d="M15 4v16M6 12h6m-3-3 3 3-3 3" />
  {:else if name === "library"}
    <rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" />
    <rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" />
  {:else if name === "filter"}<path d="M3 4h18l-7 8v6l-4 2v-8Z" />
  {:else if name === "trash"}<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6" />
  {:else if name === "develop"}
    <path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0" />
    <circle cx="16" cy="6" r="2" /><circle cx="10" cy="12" r="2" /><circle cx="18" cy="18" r="2" />
  {:else if name === "export"}
    <path d="M12 3v12M7 8l5-5 5 5" /><path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6" />
  {:else if name === "warning"}<path d="m12 3 10 18H2Z" /><path d="M12 9v5M12 17h0" />
  {:else if name === "about"}
    <circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 7h0" />
  {:else if name === "more"}<circle cx="12" cy="5" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="12" cy="19" r="1" />
  {:else if name === "camera"}<path d="M3 7h4l2-3h6l2 3h4v13H3Z" /><circle cx="12" cy="13" r="4" />
  {:else if name === "film"}<rect x="3" y="3" width="18" height="18" rx="2" /><path d="M7 3v18M17 3v18M3 8h4M3 16h4M17 8h4M17 16h4" />
  {:else if name === "clock"}<circle cx="12" cy="12" r="9" /><path d="M12 7v5l4 2" />
  {:else if name === "frames"}<rect x="7" y="3" width="14" height="14" rx="2" /><path d="M3 7v12a2 2 0 0 0 2 2h12M10 7h8M10 11h5" />
  {:else if name === "levels"}<path d="M4 4v16h16M8 16v-4M12 16V6M16 16V9M20 16v-2" />
  {:else if name === "analytics"}<path d="M4 20V9M10 20V4M16 20v-7M22 20H2" />
  {:else if name === "metadata"}<rect x="3" y="3" width="18" height="18" rx="2" /><path d="M7 8h2m3 0h5M7 12h2m3 0h5M7 16h2m3 0h5" />
  {:else if name === "notes"}<path d="M6 3h12a2 2 0 0 1 2 2v16l-4-3H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z" /><path d="M8 7h8M8 11h8M8 15h4" />
  {:else if name === "folder"}<path d="M3 7V5h7l2 3h9v12H3Z" /><path d="M3 9h18" />
  {:else if name === "mount" || name === "unmount" || name === "media"}<rect x="3" y="14" width="18" height="7" rx="2" /><path d="M6 18h1m4 0h7" />{#if name === "mount"}<path d="M12 3v8M8 7l4 4 4-4" />{:else if name === "unmount"}<path d="m8 10 4-6 4 6ZM8 12h8" />{/if}
  {:else if name === "import"}<path d="M12 3v12M7 10l5 5 5-5M4 16v5h16v-5" />
  {:else if name === "reset"}<path d="M4 9a8 8 0 1 1 1 9M3 3v7h7" />
  {:else if name === "vignette"}<rect x="3" y="4" width="18" height="16" rx="3" /><circle cx="12" cy="12" r="4" /><path d="M6 7h0m12 10h0" />
  {:else if name === "replay"}<path d="M6.35 6.35A8 8 0 1 1 4 12" /><path d="M6.35 2.85v3.5h3.5" />
  {:else if name === "zoom"}<circle cx="10" cy="10" r="6" /><path d="m15 15 6 6M7.5 10h5M10 7.5v5" />
  {:else if name === "overlay"}<rect x="3" y="4" width="18" height="16" rx="2" /><path d="m6 15 4-4 3 3 5-6" />
  {:else if name === "off"}<circle cx="12" cy="12" r="8" /><path d="m6.5 17.5 11-11" />
  {:else if name === "chevron"}<path d="m9 6 6 6-6 6" />
  {:else if name === "location"}<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11Z" /><circle cx="12" cy="10" r="2.3" />
  {:else if name === "gyro"}<path d="M20 12a8 8 0 1 1-3-6.2" /><path d="M20 4v5h-5" /><circle cx="12" cy="12" r="2" />
  {:else if name === "acceleration"}<path d="M3 12h13M12 7l5 5-5 5M20 5v14" />
  {:else if name === "balance"}<path d="M12 3v10" /><circle cx="12" cy="17" r="4" /><path d="M15 6h3M15 10h3" />
  {:else if name === "noise"}<path d="M3 12h2l2-6 3 12 3-9 2 6 2-3h4" />
  {:else if name === "shutter"}<circle cx="12" cy="12" r="9" /><path d="m14.3 3.3-5 8.7M20.6 9.5h-10M18.6 18l-5-8.7M9.7 20.7l5-8.7M3.4 14.5h10M5.4 6l5 8.7" />
  {:else if name === "focus"}<path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4" /><circle cx="12" cy="12" r="3" />
  {:else if name === "black"}<circle cx="12" cy="12" r="8" /><path d="M12 4a8 8 0 0 0 0 16Z" fill="currentColor" />
  {:else if name === "white"}<circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5" />
  {:else}
    <circle cx="12" cy="12" r="3" />
    <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1" />
  {/if}
</svg>
