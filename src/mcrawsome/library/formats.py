"""Recording formats, and the files and folders library scans leave out."""

from fnmatch import fnmatch
from pathlib import Path

from mcrawsome.io.directlog import VIDEO_SUFFIXES

SUFFIXES = (".mcraw", *VIDEO_SUFFIXES)
IGNORED = (".*", "~*", "*.tmp", "*.part")
# DaVinci Resolve's own folders in its media storage location (often ~/Videos): its render cache, proxy
# media and project backups hold no recordings, and Resolve writes to them while it is open.
EDITOR_FOLDERS = frozenset({"CacheClip", "ProxyMedia", "Resolve Project Backups"})


def scanned_folder(path: Path) -> bool:
    """Whether a library folder's subfolder is scanned and watched: neither hidden (Resolve's `.gallery`
    included) nor one of an editor's own folders."""
    return not any(fnmatch(path.name, pattern) for pattern in IGNORED) and path.name not in EDITOR_FOLDERS
