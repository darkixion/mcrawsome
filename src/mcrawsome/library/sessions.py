"""Named original recordings, complete per-clip settings and workspace state."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mcrawsome.errors import McError
from mcrawsome.io.atomic import write_atomic
from mcrawsome.library.annotations import ClipAnnotations, read_annotations
from mcrawsome.library.formats import SUFFIXES
from mcrawsome.library.opened import OpenedClips
from mcrawsome.library.scan import ClipSummary, LibraryView, clip_id
from mcrawsome.paths import SESSION_SUFFIX, state_dir
from mcrawsome.recipe.clip_layer import read_layer
from mcrawsome.recipe.load import RecipeError, build_recipe, load_toml, lower_layer_paths
from mcrawsome.recipe.schema import Recipe
from mcrawsome.recipe.session_state import session_path

_LIMIT = 16 * 1024 * 1024


class ClipView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overlay: Literal[
        "off",
        "gyro",
        "acceleration",
        "timing",
        "wb",
        "shading",
        "noise_scale",
        "noise_offset",
        "iso",
        "shutter",
        "blur",
        "focus",
        "black",
        "white",
    ] = "off"
    resolution: Literal["adaptive", "source"] = "adaptive"


class ClipSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Path
    source_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    recipe: Recipe
    annotations: ClipAnnotations


class Workspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[Path] = Field(default_factory=list, max_length=4096)
    selection: list[str] = Field(default_factory=list, max_length=4096)
    focused: str | None = None
    views: dict[str, ClipView] = Field(default_factory=dict, max_length=4096)
    search: str = Field(default="", max_length=512)
    kind: Literal["all", "raw", "log"] = "all"
    sort: Literal["captured", "name"] = "captured"
    facets: dict[str, str] = Field(default_factory=dict, max_length=16)  # one per Library filter

    @field_validator("paths")
    @classmethod
    def media_paths(cls, paths: list[Path]) -> list[Path]:
        if any(path.suffix.lower() not in SUFFIXES for path in paths):
            raise ValueError("Sessions contain recording or video paths only")
        return list(dict.fromkeys(paths))


class SessionDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["mcrawsome.session"] = "mcrawsome.session"
    schema_version: Literal[1, 2] = 2
    name: str = Field(default="Untitled", max_length=512)
    portable: bool = False
    workspace: Workspace = Field(default_factory=Workspace)
    snapshots: list[ClipSnapshot] = Field(default_factory=list, max_length=4096)


class SessionState(BaseModel):
    document: SessionDocument = Field(default_factory=SessionDocument)
    path: Path | None = None
    recent: list[Path] = Field(default_factory=list, max_length=12)


class SessionView(BaseModel):
    name: str
    path: Path | None
    portable: bool
    workspace: Workspace
    clips: list[ClipSummary]
    missing: list[Path]
    recent: list[Path]
    problem: str | None = None


def _read(path: Path) -> SessionDocument:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _LIMIT:
            raise ValueError("Choose a session file smaller than 16 MiB")
        # Require the format marker, rather than accepting a document made entirely of defaults.
        data = json.loads(path.read_bytes())
        if not isinstance(data, dict) or data.get("format") != "mcrawsome.session":
            raise ValueError("This is not an MCRAWsome session")
        document = SessionDocument.model_validate(data)
        document.format = "mcrawsome.session"
        document.workspace.paths = [(path.parent / item).resolve() for item in document.workspace.paths]
        for snapshot in document.snapshots:
            snapshot.path = (path.parent / snapshot.path).resolve()
            if snapshot.path not in document.workspace.paths:
                raise ValueError("Clip settings refer to a recording outside this session")
        if len({item.path for item in document.snapshots}) != len(document.snapshots):
            raise ValueError("Duplicate clip settings")
        return document
    except (OSError, ValueError, ValidationError) as exc:
        raise McError(f"Could not load session: {exc}", code="input-unreadable") from None


class Sessions:
    def __init__(self) -> None:
        self.lock = RLock()
        self.record = state_dir() / "current-session.json"
        self.opened = OpenedClips(record=state_dir() / "session-facts.json")
        self.state = SessionState()
        self.problem: str | None = None
        self.applied: set[Path] = set()
        try:
            if self.record.stat().st_size > _LIMIT:
                raise ValueError("Saved workspace exceeds 16 MiB")
            self.state = SessionState.model_validate_json(self.record.read_bytes())
            if any(not path.is_absolute() for path in self.state.document.workspace.paths):
                raise ValueError("Saved workspace contains relative paths")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, ValidationError) as exc:
            self.problem = (
                f"Could not restore the workspace: {exc}. Load a session or choose New to continue."
            )
        self.opened.paths = self.state.document.workspace.paths.copy()

    def view(self) -> SessionView:
        with self.lock:
            document = self.state.document
            try:
                self._restore(document)
            except (McError, OSError, ValueError) as exc:
                self.problem = (
                    f"Could not restore clip settings: {exc}. Load a session or choose New to continue."
                )
            return SessionView(
                name=document.name,
                path=self.state.path,
                portable=document.portable,
                workspace=document.workspace,
                clips=self.opened.refresh().clips,
                missing=[path for path in document.workspace.paths if not path.is_file()],
                recent=self.state.recent,
                problem=self.problem,
            )

    def _capture(self, document: SessionDocument) -> None:
        previous = {item.path: item for item in document.snapshots}
        snapshots = []
        for path in document.workspace.paths:
            if not path.is_file():
                if path in previous:
                    snapshots.append(previous[path])
                continue
            identity = clip_id(path)
            if path in previous and previous[path].source_id != identity:
                raise McError(
                    f"The recording changed: {path}. Start a new session to use its replacement.",
                    code="clip-missing",
                )
            layer = read_layer(path)
            if layer.error:
                raise McError(layer.error, code="recipe-invalid")
            try:
                recipe = build_recipe([*[load_toml(item) for item in lower_layer_paths(path)], layer.values])
            except (RecipeError, OSError) as exc:
                raise McError(f"Could not capture clip settings: {exc}", code="recipe-invalid") from None
            snapshots.append(
                ClipSnapshot(
                    path=path,
                    source_id=identity,
                    recipe=recipe,
                    annotations=read_annotations(path, identity).values,
                )
            )
        document.snapshots = snapshots
        document.schema_version = 2
        self.applied.update(item.path for item in snapshots if item.path.is_file())

    def _restore(self, document: SessionDocument) -> dict[Path, bytes | None]:
        pending = [
            item
            for item in document.snapshots
            if item.path in document.workspace.paths and item.path not in self.applied and item.path.is_file()
        ]
        # Validate every identity before replacing any saved settings.
        for item in pending:
            if clip_id(item.path) != item.source_id:
                raise McError(
                    f"The recording changed: {item.path}. Session settings were not applied.",
                    code="clip-missing",
                )
        writes: dict[Path, bytes] = {}
        for item in pending:
            writes[session_path(item.path)] = tomli_w.dumps(
                item.recipe.model_dump(mode="json", exclude_none=True)
            ).encode()
            record = read_annotations(item.path, item.source_id)
            document_data = record.document.copy()
            document_data["details"] = {
                key: value
                for key, value in document_data["details"].items()
                if key not in ClipAnnotations.model_fields
            } | item.annotations.model_dump(mode="json", exclude_defaults=True)
            # Local session state includes empty details to mask a different adjacent sidecar.
            writes[state_dir() / "clip-details" / f"{item.source_id}.meta"] = (
                json.dumps(document_data, ensure_ascii=False, indent=2) + "\n"
            ).encode()
        before: dict[Path, bytes | None] = {}
        try:
            for target, raw in writes.items():
                if target.is_symlink():
                    raise OSError(f"Refusing a symbolic link: {target}")
                before[target] = target.read_bytes() if target.exists() else None
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_atomic(target, raw)
        except OSError as exc:
            for target, previous_raw in reversed(list(before.items())):
                if previous_raw is None:
                    target.unlink(missing_ok=True)
                else:
                    write_atomic(target, previous_raw)
            raise McError(f"Could not restore clip settings: {exc}", code="output-unwritable") from None
        self.applied.update(item.path for item in pending)
        return before

    def _persist(self, state: SessionState) -> SessionView:
        try:
            self.record.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            raw = state.model_dump_json(indent=2).encode() + b"\n"
            if len(raw) > _LIMIT:
                raise McError("Workspace exceeds 16 MiB", code="output-unwritable")
            write_atomic(self.record, raw)
        except OSError as exc:
            raise McError(f"Could not auto-save the workspace: {exc}", code="output-unwritable") from None
        self.state = state
        self.opened.paths = state.document.workspace.paths.copy()
        self.problem = None
        return self.view()

    def update(self, workspace: Workspace) -> SessionView:
        with self.lock:
            if self.problem:
                raise McError(self.problem, code="input-unreadable")
            workspace = workspace.model_copy(
                update={"paths": [path.expanduser().resolve() for path in workspace.paths]}
            )
            state = self.state.model_copy(deep=True)
            state.document.workspace = workspace
            self._restore(state.document)
            self._capture(state.document)
            if state.path is not None:
                self._save_document(state)
            return self._persist(state)

    def clip_changed(self, path: Path) -> None:
        """Keep the active session snapshot current when the clip autosave finishes."""
        with self.lock:
            if path.resolve() not in self.state.document.workspace.paths or self.problem:
                return
            try:
                self.update(self.state.document.workspace.model_copy(deep=True))
            except (McError, OSError, ValueError) as exc:
                # The clip write itself succeeded. Report the separate session failure in its menu.
                self.problem = f"Clip details saved, but the session could not be updated: {exc}"

    def open_files(self, paths: list[Path]) -> LibraryView:
        with self.lock:
            before = self.opened.paths.copy()
            try:
                view = self.opened.add(paths)
                workspace = self.state.document.workspace.model_copy(deep=True)
                workspace.paths = self.opened.paths.copy()
                self.update(workspace)
                return view
            except BaseException:
                self.opened.paths = before
                raise

    def remove_files(self, paths: list[Path]) -> LibraryView:
        with self.lock:
            removed = {path.expanduser().resolve() for path in paths}
            workspace = self.state.document.workspace.model_copy(deep=True)
            workspace.paths = [path for path in workspace.paths if path not in removed]
            remaining = {
                clip.id
                for clip in self.opened.refresh().clips
                if not removed.intersection(
                    [Path(clip.path), *map(Path, clip.paths), *map(Path, clip.canonical_paths)]
                )
            }
            workspace.selection = [identity for identity in workspace.selection if identity in remaining]
            if workspace.focused not in remaining:
                workspace.focused = None
            self.update(workspace)
            return self.opened.refresh()

    def close_files(self) -> LibraryView:
        with self.lock:
            workspace = self.state.document.workspace.model_copy(deep=True)
            workspace.paths = []
            workspace.selection = []
            workspace.focused = None
            self.update(workspace)
            return self.opened.refresh()

    def new(self) -> SessionView:
        with self.lock:
            self.applied.clear()
            return self._persist(SessionState(recent=self.state.recent))

    def load(self, path: Path) -> SessionView:
        with self.lock:
            path = path.expanduser().absolute()
            document = _read(path)
            applied = self.applied.copy()
            previous_state = self.state
            before: dict[Path, bytes | None] = {}
            self.applied.clear()
            try:
                before = self._restore(document)
                recent = [path, *(item for item in self.state.recent if item != path)][:12]
                return self._persist(SessionState(document=document, path=path, recent=recent))
            except BaseException:
                self.applied = applied
                self.state = previous_state
                self.opened.paths = previous_state.document.workspace.paths.copy()
                for target, raw in reversed(list(before.items())):
                    if raw is None:
                        target.unlink(missing_ok=True)
                    else:
                        write_atomic(target, raw)
                raise

    def _save_document(self, state: SessionState) -> None:
        path = state.path
        assert path is not None
        if path.exists() or path.is_symlink():
            _read(path)
        document = state.document.model_copy(deep=True)
        if document.portable:
            for snapshot in document.snapshots:
                snapshot.path = Path(os.path.relpath(snapshot.path, path.parent))
            document.workspace.paths = [
                Path(os.path.relpath(item, path.parent)) for item in document.workspace.paths
            ]
        raw = document.model_dump_json(indent=2).encode() + b"\n"
        if len(raw) > _LIMIT:
            raise McError("Session exceeds 16 MiB", code="output-unwritable")
        try:
            write_atomic(path, raw)
        except OSError as exc:
            raise McError(f"Could not save session: {exc}", code="output-unwritable") from None

    def save(self, path: Path | None, portable: bool | None) -> SessionView:
        with self.lock:
            if self.problem:
                raise McError(self.problem, code="input-unreadable")
            path = path or self.state.path
            if path is None:
                raise McError("Choose where to save the session", code="output-unwritable")
            path = path.expanduser().absolute()
            if not path.name.endswith(SESSION_SUFFIX):
                raise McError(f"Use the {SESSION_SUFFIX} suffix", code="output-unwritable")
            if path.exists() or path.is_symlink():
                _read(path)  # Never replace another application's file or a recording.
            state = self.state.model_copy(deep=True)
            state.path = path
            state.document.name = path.name.removesuffix(SESSION_SUFFIX)
            if portable is not None:
                state.document.portable = portable
            self._capture(state.document)
            self._save_document(state)
            state.recent = [path, *(item for item in state.recent if item != path)][:12]
            return self._persist(state)
