"""Portable editorial details, versioned independently of recorded metadata and Recipes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mcrawsome.errors import McError
from mcrawsome.io.atomic import write_atomic
from mcrawsome.library.scan import ChangingError, clip_id
from mcrawsome.paths import state_dir

_FORMAT = "mcrawsome.clip-details"
_LIMIT = 256 * 1024
Label = Annotated[str, Field(max_length=256)]
Keyword = Annotated[str, Field(max_length=128)]


class ClipAnnotations(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(default="", max_length=512)
    description: str = Field(default="", max_length=16384)
    keywords: list[Keyword] = Field(default_factory=list, max_length=64)
    scene: Label = ""
    shot: Label = ""
    take: Label = ""
    location: str = Field(default="", max_length=512)
    tag: Literal["untagged", "good-take", "rejected"] = "untagged"
    rating: int = Field(default=0, ge=0, le=5)
    notes: str = Field(default="", max_length=16384)
    camera: Label = ""

    @field_validator("*")
    @classmethod
    def text(cls, value: Any) -> Any:
        for text in value if isinstance(value, list) else [value]:
            if isinstance(text, str) and any(
                (ord(c) < 32 and c not in "\n\t") or 0xD800 <= ord(c) <= 0xDFFF or ord(c) in (0xFFFE, 0xFFFF)
                for c in text
            ):
                raise ValueError("Clip details contain an unsupported control character")
        return value


class AnnotationRecord(BaseModel):
    values: ClipAnnotations
    version: str
    storage: Literal["sidecar", "local"]
    message: str
    document: dict[str, Any]


def _document(identity: str) -> dict[str, Any]:
    return {"format": _FORMAT, "schema_version": 1, "source_id": identity, "details": {}}


def _values(data: dict[str, Any]) -> ClipAnnotations:
    details = {key: value for key, value in data["details"].items() if key in ClipAnnotations.model_fields}
    try:
        return ClipAnnotations.model_validate(details)
    except ValidationError as exc:
        raise McError(f"Invalid saved clip details: {exc}", code="input-unreadable") from None


def _read(path: Path, identity: str) -> tuple[dict[str, Any], str] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        raw = stream.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        return None
    try:
        data = json.loads(raw)
        json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("format") != _FORMAT or data.get("source_id") != identity:
        return None
    if (
        type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or not isinstance(data.get("details"), dict)
    ):
        raise McError(
            "This clip metadata uses an unsupported format; it was left unchanged",
            code="input-unreadable",
        )
    _values(data)
    return data, hashlib.sha256(raw).hexdigest()


def _minimal(document: dict[str, Any], values: ClipAnnotations) -> dict[str, Any]:
    return {
        **document,
        "format": _FORMAT,
        "details": {
            key: value
            for key, value in document["details"].items()
            if key not in ClipAnnotations.model_fields
        }
        | values.model_dump(mode="json", exclude_defaults=True),
    }


def _empty(document: dict[str, Any]) -> bool:
    return not document["details"] and set(document) == {"format", "schema_version", "source_id", "details"}


def _record(source: Path, identity: str) -> tuple[AnnotationRecord, Path]:
    local = state_dir() / "clip-details" / f"{identity}.meta"
    stored = _read(local, identity)
    if stored is None and (local.exists() or local.is_symlink()):
        raise McError(
            "Saved local clip details could not be read; the file was left unchanged", code="input-unreadable"
        )
    beside = source.with_suffix(".meta")
    sidecar = None if stored else _read(beside, identity)
    writable = bool(source.parent.stat().st_mode & 0o222) and os.access(source.parent, os.W_OK)
    occupied = beside.exists() or beside.is_symlink()
    if stored:
        data, version = stored
        storage: Literal["sidecar", "local"] = "local"
    elif sidecar:
        data, version = sidecar
        storage = "sidecar" if writable else "local"
    else:
        data, version = _document(identity), ""
        storage = "sidecar" if writable and not occupied else "local"
    message = (
        "Details save beside the recording as " + beside.name
        if storage == "sidecar"
        else "Details save in MCRAWsome. Save metadata to carry these details with the recording."
    )
    if not stored and occupied and sidecar is None:
        message += " The existing metadata file is left untouched."
    values = _values(data)
    return AnnotationRecord(
        values=values, version=version, storage=storage, message=message, document=data
    ), beside if storage == "sidecar" else local


def _source(source: Path, identity: str) -> Path:
    try:
        source = source.resolve(strict=True)
        if clip_id(source) != identity:
            raise McError(
                "The recording changed; refresh the Library before editing its details", code="clip-missing"
            )
    except (OSError, ChangingError) as exc:
        raise McError(f"The recording is unavailable: {exc}", code="clip-missing") from None
    return source


def read_annotations(source: Path, identity: str) -> AnnotationRecord:
    source = _source(source, identity)
    return _record(source, identity)[0]


def write_annotations(
    source: Path, identity: str, values: ClipAnnotations, expected: str
) -> AnnotationRecord:
    source = _source(source, identity)
    folder = state_dir() / "clip-details"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current, path = _record(source, identity)
        if current.version != expected:
            raise McError(
                "Clip details changed elsewhere. Your edits have been kept; "
                "reload saved details before replacing them.",
                code="recipe-changed",
            )
        document = _minimal(current.document, values)
        raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(raw) > _LIMIT:
            raise McError("Clip metadata exceeds 256 KiB", code="recipe-invalid")
        try:
            if _empty(document) and current.storage == "sidecar":
                path.unlink(missing_ok=True)
            elif document["details"] or current.version:
                write_atomic(path, raw)
        except OSError as exc:
            raise McError(f"Could not save clip details: {exc}", code="output-unwritable") from None
        return _record(source, identity)[0]


def search_text(values: ClipAnnotations) -> str:
    """The clip's own words that a Library search matches, one per line."""
    words = [values.title, values.description, *values.keywords, values.notes, values.scene, values.shot]
    words += [values.take, values.location, values.camera]
    return "\n".join(word for word in words if word)


def details_for_search(source: Path, identity: str) -> ClipAnnotations | None:
    """A listed clip's details, using the scan's known identity, without hashing the clip again; None
    when they cannot be read."""
    try:
        return _record(source, identity)[0].values
    except (McError, OSError):
        return None


def details_for_summary(source: Path, identity: str) -> tuple[int, str]:
    """Editorial stars and searchable words, using the scan's known identity, without hashing the clip
    again."""
    values = details_for_search(source, identity)
    return (0, "") if values is None else (values.rating, search_text(values))


def annotations_for(source: Path, identity: str) -> ClipAnnotations:
    """A clip's saved details using the scan's known identity (empty when none or unreadable)."""
    try:
        return _record(source, identity)[0].values
    except (McError, OSError):
        return ClipAnnotations()
