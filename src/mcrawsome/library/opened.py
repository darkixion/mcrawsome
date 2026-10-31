"""Engine-session file references, separate from watched folders and their scan cache."""

from pathlib import Path
from threading import RLock

from mcrawsome.errors import McError
from mcrawsome.io.directlog import DirectLogError, is_video_input, probe_direct_log, require_log_video
from mcrawsome.library.formats import SUFFIXES
from mcrawsome.library.scan import Library, LibraryView
from mcrawsome.output.ffmpeg import find_ffmpeg
from mcrawsome.paths import NAME, state_dir


class OpenedClips:
    def __init__(self, *, record: Path | None = None) -> None:
        self.library = Library(record=record or state_dir() / "opened-facts.json")
        self.paths: list[Path] = []
        self.lock = RLock()

    def add(self, paths: list[Path]) -> LibraryView:
        checked = []
        for path in paths:
            path = path.expanduser().absolute()
            if not path.is_file() or path.suffix.lower() not in SUFFIXES:
                raise McError(
                    f"Choose a MotionCam recording or video file: {path.name}", code="input-unreadable"
                )
            if is_video_input(path):
                try:
                    require_log_video(probe_direct_log(find_ffmpeg()[1], path))
                except DirectLogError as exc:
                    raise McError(str(exc), code="input-unreadable") from None
            checked.append(path.resolve())
        with self.lock:
            combined = list(dict.fromkeys([*self.paths, *checked]))
            if len(combined) > 256:
                raise McError("Open at most 256 temporary clips", code="input-unreadable")
            self.paths = combined
            return self.refresh()

    def refresh(self) -> LibraryView:
        with self.lock:
            return self.library.scan_files(self.paths)

    def clear(self) -> LibraryView:
        with self.lock:
            self.paths = []
            return self.refresh()

    def remove(self, paths: list[Path]) -> LibraryView:
        removed = {path.expanduser().resolve() for path in paths}
        with self.lock:
            self.paths = [path for path in self.paths if path not in removed]
            return self.refresh()

    def permitted(self) -> list[Path]:
        with self.lock:
            return [*self.paths, *(path.parent / NAME for path in self.paths)]
