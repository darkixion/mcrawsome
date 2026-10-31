"""Layering, overrides and friendly validation for Recipes."""

from __future__ import annotations

import difflib
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import tomli_w
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from mcrawsome.paths import NAME, config_dir
from mcrawsome.recipe.schema import Recipe


class RecipeError(Exception):
    """One or more Recipe problems, described in plain language."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def recipe_fields(
    model: type[BaseModel] = Recipe, prefix: str = ""
) -> list[tuple[str, FieldInfo, type[Any]]]:
    out: list[tuple[str, FieldInfo, type[Any]]] = []
    for name, info in model.model_fields.items():
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            out.extend(recipe_fields(annotation, f"{prefix}{name}."))
        else:
            out.append((f"{prefix}{name}", info, annotation if isinstance(annotation, type) else str))
    return out


@dataclass(frozen=True)
class FieldDoc:
    name: str
    kind: str  # "number", "whole number", "yes/no", "text" or "choice"
    allowed: str  # the range or the choices
    default: str
    description: str
    layer: int


def field_layer(info: FieldInfo) -> int:
    """1 for the everyday settings, 2 for the common ones, 3 for the fine controls."""
    extra = info.json_schema_extra
    layer = extra.get("layer", 3) if isinstance(extra, dict) else 3
    return layer if isinstance(layer, int) else 3


def _bounds(metadata: Sequence[Any]) -> str:
    low = high = None
    for item in metadata:
        if hasattr(item, "ge"):
            low = f"{item.ge:g}"
        elif hasattr(item, "gt"):
            low = f"above {item.gt:g}"
        if hasattr(item, "le"):
            high = f"{item.le:g}"
    if low is not None and high is not None:
        return f"{low} to {high}"
    if low is not None:
        return f"{low} or more"
    return f"up to {high}" if high is not None else ""


def _describe_type(annotation: Any, metadata: Sequence[Any]) -> list[tuple[str, str]]:
    """(kind, allowed) pairs for an annotation, one per alternative of a union."""
    origin = get_origin(annotation)
    if origin is Annotated:
        inner, *extra = get_args(annotation)
        flat = [m for e in extra for m in (e.metadata if isinstance(e, FieldInfo) else [e])]
        return _describe_type(inner, [*metadata, *flat])
    if origin is Literal:
        return [("choice", ", ".join(str(v) for v in get_args(annotation)))]
    if origin is Union or isinstance(annotation, type(int | str)):
        parts = [a for a in get_args(annotation) if a is not type(None)]
        return [pair for part in parts for pair in _describe_type(part, metadata)]
    kind = {int: "whole number", float: "number", bool: "yes/no"}.get(annotation, "text")
    return [(kind, _bounds(metadata))]


def field_docs() -> list[FieldDoc]:
    """Every Recipe setting with its type, allowed values, default and description (for --help-all)."""
    docs = []
    for name, info, _ in recipe_fields():
        pairs = _describe_type(info.annotation, info.metadata)
        kinds = " or ".join(dict.fromkeys(kind for kind, _ in pairs))
        allowed = ", ".join(a for _, a in pairs if a)
        default = "unset" if info.default is None else str(info.default)
        if isinstance(info.default, bool):
            default = "yes" if info.default else "no"
        docs.append(FieldDoc(name, kinds, allowed, default, info.description or "", field_layer(info)))
    return docs


def _deep_merge(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in top.items():
        if key == "develop" and isinstance(value, dict):
            # Legacy clip/folder overrides still win over a newly saved recorded/static default.
            value = dict(value)
            if {"black_level", "white_level"} & value.keys() and "level_policy" not in value:
                value["level_policy"] = "legacy"
            if "mount_camera_model" in value and "camera_model_source" not in value:
                value["camera_model_source"] = "override"
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _describe(error: Any, model: type[BaseModel] = Recipe) -> str:
    key = ".".join(str(part) for part in error["loc"])
    if error["type"] == "extra_forbidden":
        known = [name for name, _, _ in recipe_fields(model)]
        close = difflib.get_close_matches(key, known, n=1)
        hint = f" Did you mean {close[0]}?" if close else ""
        return f"{key}: unknown setting.{hint}"
    ctx = error.get("ctx") or {}
    info = dict(recipe_fields_by_name(model)).get(key)
    if info is not None and (info.metadata or ctx):
        bounds = [str(getattr(m, attr)) for m in info.metadata for attr in ("ge", "le") if hasattr(m, attr)]
        if bounds:
            return f"{key}: {error['msg']} (allowed range {bounds[0]} to {bounds[-1]})"
    message = str(error["msg"]).removeprefix("Value error, ")
    return f"{key}: {message}" if key else message


def recipe_fields_by_name(model: type[BaseModel] = Recipe) -> list[tuple[str, FieldInfo]]:
    return [(name, info) for name, info, _ in recipe_fields(model)]


def build_recipe(layers: Sequence[dict[str, Any]]) -> Recipe:
    return validate_layers(layers, Recipe)


def validate_layers[R: Recipe](layers: Sequence[dict[str, Any]], model: type[R]) -> R:
    """The layers merged in order and validated as `model` (the Recipe, or a Recipe with more tables)."""
    merged: dict[str, Any] = {}
    for layer in layers:
        merged = _deep_merge(merged, layer)
    try:
        return model.model_validate(merged)
    except ValidationError as exc:
        problems = "\n".join(f"  - {_describe(e, model)}" for e in exc.errors())
        location = ".".join(str(part) for part in exc.errors()[0]["loc"])
        # Union alternatives add type labels to the location after the setting's name.
        known = [
            name for name, _, _ in recipe_fields(model) if location == name or location.startswith(name + ".")
        ]
        field = max(known, key=len) if known else location
        raise RecipeError(f"Recipe problems:\n{problems}", field=field or None) from None


def _coerce(text: str) -> Any:
    for parse in (int, float):
        try:
            return parse(text)
        except ValueError:
            pass
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    return text


def parse_override(text: str) -> dict[str, Any]:
    if "=" not in text:
        raise RecipeError(f"override {text!r} must look like key=value, e.g. develop.exposure=1.5")
    key, value = text.split("=", 1)
    node: dict[str, Any] = {}
    parts = key.strip().split(".")
    cursor = node
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = _coerce(value.strip())
    return node


def load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RecipeError(f"{path}: not valid TOML ({exc})") from None


def folder_layer_path(folder: Path) -> Path:
    """A folder's own settings: mcrawsome.toml."""
    return folder / f"{NAME}.toml"


def lower_layer_paths(clip: Path) -> list[Path]:
    """The defaults and folder Recipe, below the clip's own settings."""
    from mcrawsome.recipe.session_state import session_override

    if session_override(clip):
        return []
    candidates = [config_dir() / "defaults.toml", folder_layer_path(clip.parent)]
    return [p for p in candidates if p.exists()]


def default_layer_paths(clip: Path) -> list[Path]:
    from mcrawsome.recipe.clip_layer import path_for

    paths = lower_layer_paths(clip)
    clip_layer = path_for(clip)
    return [*paths, clip_layer] if clip_layer.exists() else paths


def recipe_to_toml(recipe: Recipe) -> str:
    return tomli_w.dumps(recipe.model_dump(mode="json", exclude_none=True))


__all__ = [
    "FieldDoc",
    "RecipeError",
    "build_recipe",
    "default_layer_paths",
    "field_docs",
    "field_layer",
    "load_toml",
    "lower_layer_paths",
    "parse_override",
    "recipe_fields",
    "recipe_to_toml",
    "validate_layers",
]
