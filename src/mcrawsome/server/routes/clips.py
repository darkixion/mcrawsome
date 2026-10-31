"""A clip's Recipe and sampled analysis, addressed through the library."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mcrawsome import __version__
from mcrawsome.analysis.run import analyse_report
from mcrawsome.errors import McError, error_code
from mcrawsome.library.scan import ClipSummary, clip_recipe
from mcrawsome.mount.manager import MountManager
from mcrawsome.paths import cache_dir
from mcrawsome.pipeline.priority import background_priority
from mcrawsome.recipe.clip_layer import ClipLayer, read_layer, write_layer
from mcrawsome.recipe.defaults import apply_defaults, defaults_path, read_defaults
from mcrawsome.recipe.load import FieldDoc, RecipeError, field_docs, load_toml, lower_layer_paths
from mcrawsome.recipe.schema import Recipe
from mcrawsome.server.config import is_loopback
from mcrawsome.server.routes.errors import ERROR_RESPONSES, RecipeConflict
from mcrawsome.server.routes.vignette import check_profile_change
from mcrawsome.trash import companions, move_to_trash, split_parts

Changes = queue.SimpleQueue[dict[str, Any]]
logger = logging.getLogger(__name__)
ANALYSIS_FORMAT = 2  # part of the cache key: raised whenever the cached document gains or changes a field


class AnalysisDocument(BaseModel):
    """`GET /clips/{id}/analysis` when it is ready: the report's sections (title to rows of label and
    text) beside these fields."""

    model_config = ConfigDict(extra="allow")

    warnings: list[str]
    suggestions: list[str]
    suggested_exposure: float | None  # stops, as automatic exposure would choose them


class AnalysisRunning(BaseModel):
    """`GET /clips/{id}/analysis` while it is measured; `event: analysis` follows."""

    state: Literal["running"] = "running"


def prune_analysis(folder: Path, max_files: int = 200, max_bytes: int = 500 * 1024 * 1024) -> None:
    """Keep the newest reports within both cache limits."""
    files = []
    for path in folder.glob("*.json"):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        files.append((stat.st_mtime_ns, stat.st_size, path))
    total = 0
    for index, (_, size, path) in enumerate(sorted(files, reverse=True)):
        total += size
        if index >= max_files or total > max_bytes:
            path.unlink(missing_ok=True)


class AnalysisHost:
    """One analysis at a time per server; concurrent requests for the same key share the work."""

    def __init__(self, find: Callable[[str], ClipSummary], workers: int | Callable[[], int]) -> None:
        self.find = find
        self.workers = workers
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="analysis", initializer=background_priority
        )
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._errors: dict[str, McError] = {}
        self._subscribers: set[Changes] = set()

    def subscribe(self) -> Changes:
        changes: Changes = queue.SimpleQueue()
        with self._lock:
            self._subscribers.add(changes)
        return changes

    def unsubscribe(self, changes: Changes) -> None:
        with self._lock:
            self._subscribers.discard(changes)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def request(self, clip_id: str, recipe: Recipe) -> JSONResponse:
        values = recipe.model_dump(mode="json")
        output = values.pop("output")
        values["output"] = {"fill_dropped": output["fill_dropped"]}
        identity = json.dumps([ANALYSIS_FORMAT, __version__, clip_id, values], sort_keys=True)
        key = hashlib.sha256(identity.encode()).hexdigest()
        target = cache_dir() / "analysis" / f"{key}.json"
        with self._lock:
            try:
                report = json.loads(target.read_text())
            except (FileNotFoundError, ValueError):
                pass
            else:
                return JSONResponse(report)
            if key in self._errors:
                raise self._errors.pop(key)
            if key not in self._running:
                self._running.add(key)
                self._executor.submit(self._analyse, clip_id, recipe, key, target)
        return JSONResponse({"state": "running"}, status_code=202)

    def _analyse(self, clip_id: str, recipe: Recipe, key: str, target: Path) -> None:
        event: dict[str, Any] = {"id": clip_id, "key": key}
        try:
            clip = self.find(clip_id)
            workers = self.workers() if callable(self.workers) else self.workers
            report = analyse_report(clip.path, recipe, min(workers, 4), target.parent / "work")
            document: dict[str, Any] = {title: dict(rows) for title, rows in report.sections}
            document.update(
                warnings=report.warnings,
                suggestions=report.suggestions,
                suggested_exposure=report.suggested_exposure,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_suffix(".part")
            part.write_text(json.dumps(document))
            part.replace(target)
            prune_analysis(target.parent)
            event["report"] = document
        except Exception as exc:
            event["error"] = {"code": error_code(exc), "message": str(exc)}
        finally:
            with self._lock:
                self._running.discard(key)
                if "error" in event:
                    self._errors[key] = McError(event["error"]["message"], code=event["error"]["code"])
                for subscriber in self._subscribers:
                    subscriber.put(event)


class RecipeView(BaseModel):
    layer: ClipLayer
    resolved: Recipe
    inherited: Recipe
    fields: list[FieldDoc]
    sources: dict[str, str]
    inherited_sources: dict[str, str]


class RecipeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any]
    version: str


class TrashRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clip_ids: list[str] = Field(min_length=1, max_length=256)


class TrashResult(BaseModel):
    """What moved to the Trash (where it was), the mounts unmounted first, and other parts of split
    recordings that were not chosen and stay."""

    trashed: list[Path]
    unmounted: list[str]
    kept_parts: list[Path] = []


def clips_router(
    api: str,
    find: Callable[[str], ClipSummary],
    analysis: AnalysisHost,
    mounts: MountManager,
    changed: Callable[[Path], None] = lambda _: None,
    forget: Callable[[list[Path]], object] = lambda _: None,
) -> APIRouter:
    router = APIRouter(prefix=api)

    @router.get("/adjustment-defaults")
    def defaults() -> ClipLayer:
        try:
            return read_defaults()
        except RecipeError as exc:
            raise McError(str(exc), code="recipe-invalid") from None

    @router.put("/adjustment-defaults")
    def save_defaults(request: RecipeUpdate) -> ClipLayer:
        try:
            return apply_defaults(request.values, request.version)
        except RecipeError as exc:
            raise McError(str(exc), code="recipe-invalid") from None
        except OSError as exc:
            raise McError(f"Could not save inherited defaults: {exc}", code="output-unwritable") from None

    @router.get("/clips/{clip_id}/recipe", response_model=RecipeView, responses=ERROR_RESPONSES)
    def recipe(clip_id: str) -> dict[str, Any]:
        clip = find(clip_id)
        layer = read_layer(clip.path)
        sources: dict[str, str] = {}
        for path in lower_layer_paths(clip.path):
            name = "Application" if path == defaults_path() else "Folder"
            sources.update(dict.fromkeys(load_toml(path).get("develop", {}), name))
        inherited_sources = dict(sources)
        sources.update(dict.fromkeys(layer.values.get("develop", {}), "This clip"))
        return {
            "sources": sources,
            "inherited_sources": inherited_sources,
            "layer": layer.model_dump(),
            "resolved": clip_recipe(clip.path, layer).model_dump(mode="json"),
            "inherited": clip_recipe(clip.path, ClipLayer(values={}, version="")).model_dump(mode="json"),
            "fields": [asdict(doc) for doc in field_docs()],
        }

    @router.put(
        "/clips/{clip_id}/recipe",
        response_model=ClipLayer,
        responses=ERROR_RESPONSES | {409: {"model": RecipeConflict}},
    )
    def save_recipe(clip_id: str, request: RecipeUpdate) -> ClipLayer | JSONResponse:
        clip = find(clip_id)
        try:
            current = clip_recipe(clip.path, read_layer(clip.path)).develop.vignette_profile
            proposed = clip_recipe(
                clip.path, ClipLayer(values=request.values, version=request.version)
            ).develop.vignette_profile
            if current != proposed:
                check_profile_change(clip, proposed, mounts)
            result = write_layer(clip.path, request.values, request.version)
            changed(clip.path)
            return result
        except McError as exc:
            if exc.code == "recipe-changed":
                return JSONResponse(
                    {
                        "error": {"code": exc.code, "message": exc.message},
                        "layer": read_layer(clip.path).model_dump(),
                    },
                    status_code=409,
                )
            raise
        except RecipeError as exc:
            return JSONResponse(
                {"error": {"code": "recipe-invalid", "field": exc.field, "message": str(exc)}},
                status_code=422,
            )

    @router.get(
        "/clips/{clip_id}/analysis",
        response_model=None,
        responses=ERROR_RESPONSES | {200: {"model": AnalysisDocument}, 202: {"model": AnalysisRunning}},
    )
    def analyse(clip_id: str) -> JSONResponse:
        clip = find(clip_id)
        return analysis.request(clip_id, clip_recipe(clip.path, read_layer(clip.path)))

    @router.post("/clips/trash")
    def trash(chosen: TrashRequest, request: Request) -> TrashResult:
        """Unmount the clips' virtual media, then move every copy of each recording and its companion
        files to the Trash. Nothing moves while an editor has their media open."""
        if request.client is None or not is_loopback(request.client.host):
            raise McError("Only this computer may move recordings to the Trash", code="client-not-allowed")
        clips = [find(clip_id) for clip_id in dict.fromkeys(chosen.clip_ids)]
        recordings = list(dict.fromkeys(path for clip in clips for path in [clip.path, *clip.paths]))
        sources = {path.resolve() for path in recordings}
        affected = [m for m in mounts.listing().mounts if m.active and m.source.resolve() in sources]
        if busy := [m for m in affected if m.in_use]:
            names = ", ".join(sorted({m.source.name for m in busy}))
            raise McError(
                f"An editor has the mounted media of {names} open; close it there first", code="engine-busy"
            )
        unmounted = [mounts.unmount(m.id).id for m in affected]
        moved: list[Path] = []
        for recording in recordings:
            for path in companions(recording):
                try:
                    move_to_trash(path)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise McError(
                        f"Could not move {path.name} to the Trash: {exc.strerror or exc}",
                        code="output-unwritable",
                    ) from None
                moved.append(path)
        try:
            forget(recordings)  # the open session no longer lists them
        except (McError, OSError, ValueError) as exc:  # the files moved: report that, not this
            logger.warning("Moved recordings to the Trash, but the session still lists them: %s", exc)
        return TrashResult(trashed=moved, unmounted=unmounted, kept_parts=split_parts(recordings))

    return router
