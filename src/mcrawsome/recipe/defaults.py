"""Versioned application adjustment defaults, below folder and clip overrides."""

import fcntl
import hashlib
import os
from pathlib import Path
from typing import Any

import tomli_w

from mcrawsome.errors import McError
from mcrawsome.io.atomic import write_atomic
from mcrawsome.paths import config_dir
from mcrawsome.recipe.clip_layer import ClipLayer, _cleared
from mcrawsome.recipe.load import RecipeError, _deep_merge, build_recipe, load_toml

ADJUSTMENTS = {"look", "hdr", "develop", "effects", "denoise", "grade", "motion", "camera"}


def defaults_path() -> Path:
    return config_dir() / "defaults.toml"


def read_defaults() -> ClipLayer:
    path = defaults_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ClipLayer(values={}, version="")
    values = load_toml(path)
    build_recipe([values])
    return ClipLayer(values=values, version=hashlib.sha256(raw).hexdigest())


def apply_defaults(values: dict[str, Any], expected: str) -> ClipLayer:
    if values.keys() - ADJUSTMENTS:
        raise RecipeError("Apply to All accepts development adjustments only")
    if cleared := _cleared(values):
        raise RecipeError(f"{cleared} has no value; defaults cannot clear optional values", field=cleared)
    path = defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        current = read_defaults()
        if current.version != expected:
            raise McError(
                "Inherited defaults changed; read them again before applying", code="recipe-changed"
            )
        merged = _deep_merge(current.values, values)
        build_recipe([merged])
        raw = tomli_w.dumps(merged).encode()
        write_atomic(path, raw)
        return ClipLayer(values=merged, version=hashlib.sha256(raw).hexdigest())
    finally:
        os.close(directory)
