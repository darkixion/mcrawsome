"""File-manager actions use the same mounted-media handoff as the interface, without a window."""

import json
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

from mcrawsome.errors import ERROR_CODES, ErrorCode, McError
from mcrawsome.gui import answers, request, start_server
from mcrawsome.mount.limits import MAX_MOUNTS
from mcrawsome.mount.manager import mounted_sources


def _call(
    port: int, path: str, method: str = "GET", body: dict[str, Any] | None = None, *, timeout: float = 2
) -> dict[str, Any]:
    try:
        return request(port, path, method, body, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            with exc:
                error = json.loads(exc.read(8192))["error"]
            code = error.get("code", "internal-error")
            message = str(error["message"])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise McError(
                f"The engine could not complete the Resolve action: {exc}", code="internal-error"
            ) from None
        raise McError(
            message, code=cast(ErrorCode, code if code in ERROR_CODES else "internal-error")
        ) from None


def _ready_clips(port: int, paths: tuple[Path, ...], say: Callable[[str], None]) -> list[dict[str, Any]]:
    """The recordings as ready clips of a running engine, each once."""
    files = list(dict.fromkeys(path.resolve() for path in paths))
    if not files or len(files) > MAX_MOUNTS:
        raise McError(f"Select between 1 and {MAX_MOUNTS} RAW recordings", code="recipe-invalid")
    if any(not path.is_file() or path.suffix.lower() != ".mcraw" for path in files):
        raise McError("Choose local .mcraw recordings", code="input-unreadable")
    if not answers(port):
        say("Starting the MCRAWsome engine")
        start_server(port, [])
    # Keep the existing engine and every existing mount. The mount API reuses matching saved media.
    listing = _call(port, "/mounts")
    if not listing.get("available"):
        raise McError(str(listing.get("reason", "Virtual media is unavailable")), code="recipe-invalid")
    opened = _call(port, "/opened-clips", "POST", {"paths": list(map(str, files))}, timeout=120)
    by_path = {
        str(Path(path).resolve()): item
        for item in opened["clips"]
        for path in [item["path"], *item.get("paths", []), *item.get("canonical_paths", [])]
    }
    chosen = [by_path.get(str(path), {}) for path in files]
    if any(
        item.get("kind") != "raw" or item.get("status") != "ready" or not item.get("id") for item in chosen
    ):
        raise McError(
            "A selected recording is not ready; let copying finish and try again", code="input-unreadable"
        )
    # The library groups identical recording copies under one clip ID. Use that media once.
    return list({item["id"]: item for item in chosen}.values())


def _mount_clips(port: int, clips: list[dict[str, Any]], say: Callable[[str], None]) -> list[dict[str, Any]]:
    mounts = []
    for item in clips:
        say(f"Preparing {Path(item['path']).name}")
        mounts.append(_call(port, "/mounts", "POST", {"clip_id": item["id"]}, timeout=120))
    return mounts


def send_files(
    port: int, paths: tuple[Path, ...], mode: Literal["bin", "timeline"], say: Callable[[str], None]
) -> dict[str, Any]:
    chosen = _ready_clips(port, paths, say)
    if mode == "timeline":
        # A clip at a rate a timeline cannot take is named before anything is mounted.
        body = {"clip_ids": [item["id"] for item in chosen]}
        rates = _call(port, "/resolve/timeline/rates", "POST", body, timeout=120)
        if rates.get("message"):
            raise McError(str(rates["message"]), code="recipe-invalid")
    mounts = [mount["id"] for mount in _mount_clips(port, chosen, say)]
    say("Preparing the Resolve timeline" if mode == "timeline" else "Preparing the Resolve Media Bin")
    result = _call(port, "/resolve/mounts/send", "POST", {"mounts": mounts, "mode": mode}, timeout=3600)
    if not result.get("launched"):
        messages = list(dict.fromkeys(str(item["message"]) for item in result.get("results", [])))
        raise McError("; ".join(messages) or "Resolve handoff could not be opened", code="output-unwritable")
    say(
        f"Sent {len(mounts)} recording{'s' if len(mounts) != 1 else ''} to Resolve. "
        "Complete the import in Resolve."
    )
    return result


def mount_files(port: int, paths: tuple[Path, ...], say: Callable[[str], None]) -> list[dict[str, Any]]:
    """Mount the recordings with their saved settings, without a window or Resolve."""
    mounts = _mount_clips(port, _ready_clips(port, paths, say), say)
    folders = ", ".join(sorted({str(Path(mount["path"]).parent) for mount in mounts}))
    say(f"Mounted {len(mounts)} recording{'s' if len(mounts) != 1 else ''} in {folders}")
    return mounts


def unmount_files(port: int, paths: tuple[Path, ...], say: Callable[[str], None]) -> list[dict[str, Any]]:
    """Try every mount of the recordings the normal way, as Unmount All does: what an application has
    open stays mounted. Each attempt's result, as the unmount-all route gives it."""
    files = {path.resolve() for path in paths}
    if not answers(port):
        if not files & mounted_sources():  # an engine starting could mount sent media again: not for this
            return []
        say("Starting the MCRAWsome engine")
        start_server(port, [])
    results = []
    for mount in _call(port, "/mounts", timeout=60)["mounts"]:
        if not mount["active"] or Path(mount["source"]).resolve() not in files:
            continue
        try:
            _call(port, f"/mounts/{mount['id']}/unmount", "POST", timeout=60)
            results.append({"source": mount["source"], "ok": True, "message": "Unmounted"})
        except McError as exc:
            results.append({"source": mount["source"], "ok": False, "message": exc.message})
    return results
