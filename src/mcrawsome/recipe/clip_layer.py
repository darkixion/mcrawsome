"""A clip's own Recipe settings, saved beside it or in the state folder.

TOML has no null, so a clip's layer can only set values: it cannot clear an optional value that a lower
layer (the folder's mcrawsome.toml or defaults.toml) sets. minimal_values leaves cleared values out, and
write_layer refuses them."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel

from mcrawsome.errors import McError
from mcrawsome.io.atomic import write_atomic
from mcrawsome.output.sidecar import quick_hash
from mcrawsome.paths import NAME, state_dir
from mcrawsome.recipe.load import RecipeError, build_recipe, load_toml, lower_layer_paths


def path_for(clip: Path, first_16mib: str | None = None) -> Path:
    """Where the clip's layer is: beside it, or in the state folder when its folder is read-only.
    `first_16mib`: the clip's quick_hash when the caller knows it, so the clip is not read again."""
    from mcrawsome.recipe.session_state import session_override

    if restored := session_override(clip, first_16mib):
        return restored
    beside = clip.with_name(f"{clip.stem}.{NAME}.toml")
    if beside.exists():
        return beside
    # Missing inputs are allowed during a dry run and have no fingerprint yet.
    if not clip.exists():
        return beside
    # Check the mode as well: root's access() permits writing to a read-only folder.
    writable = bool(clip.parent.stat().st_mode & 0o222) and os.access(clip.parent, os.W_OK)
    store = state_dir() / "clip-recipes"
    if writable and not (store.is_dir() and any(store.iterdir())):
        return beside  # no saved layer can be in the state folder: skip reading the clip
    fingerprint = first_16mib if first_16mib is not None else quick_hash(clip)
    key = hashlib.sha256(f"{clip.resolve()}|{clip.stat().st_size}|{fingerprint}".encode()).hexdigest()[:20]
    fallback = store / f"{key}.toml"
    if fallback.exists():
        return fallback
    return beside if writable else fallback


class ClipLayer(BaseModel):
    values: dict[str, Any]
    version: str
    error: str | None = None


def _lower_values(clip: Path) -> list[dict[str, Any]]:
    return [load_toml(path) for path in lower_layer_paths(clip)]


def _cleared(values: dict[str, Any], prefix: str = "") -> str | None:
    """The dotted name of the first value that is None, which TOML cannot hold."""
    for key, value in values.items():
        name = f"{prefix}{key}"
        if value is None:
            return name
        if isinstance(value, dict) and (inner := _cleared(value, f"{name}.")):
            return inner
    return None


def _read(clip: Path, path: Path) -> ClipLayer:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return ClipLayer(values={}, version="")
    version = hashlib.sha256(content).hexdigest()
    values: dict[str, Any] = {}
    try:
        values = tomllib.loads(content.decode("utf-8"))
        build_recipe([*_lower_values(clip), values])
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return ClipLayer(values=values, version=version, error=f"{path}: not valid TOML ({exc})")
    except RecipeError as exc:
        return ClipLayer(values=values, version=version, error=f"{path}: {exc}")
    return ClipLayer(values=values, version=version)


def read_layer(clip: Path, first_16mib: str | None = None) -> ClipLayer:
    return _read(clip, path_for(clip, first_16mib))


def write_layer(clip: Path, values: dict[str, Any], expected: str) -> ClipLayer:
    path = path_for(clip)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock the directory, which survives replacing or deleting the Recipe file.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        current = _read(clip, path)
        if current.version != expected:
            raise McError(
                f"{path}: the Recipe has changed; read it again before saving", code="recipe-changed"
            )
        if cleared := _cleared(values):
            raise RecipeError(
                f"{path}: {cleared} has no value; a clip's Recipe can set values but not clear them",
                field=cleared,
            )
        from mcrawsome.colour.white_balance import validate_clip_white_balance

        validate_clip_white_balance(clip, build_recipe([*_lower_values(clip), values]))
        if not values and path.parent != state_dir() / "session-recipes":
            path.unlink(missing_ok=True)
            return ClipLayer(values={}, version="")
        content = tomli_w.dumps(values).encode("utf-8")
        write_atomic(path, content)
        return ClipLayer(values=values, version=hashlib.sha256(content).hexdigest())
    finally:
        os.close(directory)


def _different(values: dict[str, Any], lower: dict[str, Any]) -> dict[str, Any]:
    changed = {}
    for key, value in values.items():
        if value is None:
            continue  # TOML cannot clear a lower layer's value
        if isinstance(value, dict) and isinstance(lower.get(key), dict):
            difference = _different(value, lower[key])
            if difference:
                changed[key] = difference
        elif isinstance(value, dict):
            if inner := _different(value, {}):
                changed[key] = inner
        elif key not in lower or value != lower[key]:
            changed[key] = value
    return changed


def minimal_values(clip: Path, recipe_values: dict[str, Any]) -> dict[str, Any]:
    lower = build_recipe(_lower_values(clip)).model_dump(mode="json")
    return _different(recipe_values, lower)
