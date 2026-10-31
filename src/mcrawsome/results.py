"""Result models shared by `--json` output and the HTTP API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from mcrawsome.errors import ErrorCode


class ErrorInfo(BaseModel):
    code: ErrorCode
    message: str


class ClipResult(BaseModel):
    """What happened to one clip of an export: the files and facts, or the error."""

    clip: Path
    output: Path | None = None
    sidecar: Path | None = None
    frames: int | None = None
    audio_offset_s: float | None = None
    notes: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None


class PlannedClip(BaseModel):
    """One clip of an export --dry-run: where it would go and the Recipe it would use."""

    clip: Path
    output: Path
    missing: bool = False
    recipe: dict[str, Any]
