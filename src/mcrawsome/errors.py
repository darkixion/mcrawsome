"""Errors that carry a short code, so scripts and the API can tell failures apart."""

from __future__ import annotations

from typing import Any, Literal, get_args

ErrorCode = Literal[
    "input-missing",
    "clip-missing",
    "input-unreadable",
    "recipe-invalid",
    "recipe-changed",
    "output-exists",
    "output-unwritable",
    "tool-missing",
    "encoder-unavailable",
    "measure-failed",
    "stabilise-failed",
    "render-failed",
    "encode-failed",
    "verify-failed",
    "disk-full",
    "paused",
    "cancelled",
    "engine-busy",
    "path-not-allowed",
    "client-not-allowed",
    "host-not-allowed",
    "origin-not-allowed",
    "unauthorised",
    "internal-error",
]
ERROR_CODES: tuple[str, ...] = get_args(ErrorCode)


def _rebuild(cls: type[McError], message: str, code: ErrorCode) -> McError:
    return cls(message, code=code)


class McError(Exception):
    """A failure with a plain-language message and a code from ErrorCode. str(error) is the message."""

    def __init__(self, message: str, *, code: ErrorCode) -> None:
        super().__init__(message)
        self.message = message
        self.code: ErrorCode = code

    def __reduce__(self) -> tuple[Any, ...]:  # keyword-only code: Exception's default would lose it
        return _rebuild, (type(self), self.message, self.code)


def error_code(exc: BaseException, default: ErrorCode = "measure-failed") -> ErrorCode:
    """The code for an exception from the export machinery; `default` for one it does not know."""
    # imported here: these modules import this one
    from pydantic import ValidationError

    from mcrawsome.io.container import ContainerError
    from mcrawsome.io.directlog import DirectLogError
    from mcrawsome.output.export import is_disk_full
    from mcrawsome.output.ffmpeg import FfmpegError
    from mcrawsome.pipeline.stabilise import StabiliseError
    from mcrawsome.recipe.load import RecipeError

    if isinstance(exc, McError):
        return exc.code
    if is_disk_full(exc):
        return "disk-full"
    if isinstance(exc, RecipeError):
        return "recipe-invalid"
    if isinstance(exc, FfmpegError):
        return "tool-missing"
    if isinstance(exc, StabiliseError):
        return "stabilise-failed"
    if isinstance(exc, DirectLogError | ContainerError | ValidationError | ValueError | OSError):
        return "input-unreadable"
    return default
