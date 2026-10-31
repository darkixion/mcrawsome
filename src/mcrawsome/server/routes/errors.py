"""Error response schemas for clip Recipe and export planning routes."""

from typing import Any

from pydantic import BaseModel

from mcrawsome.recipe.clip_layer import ClipLayer
from mcrawsome.results import ErrorInfo


class FieldError(ErrorInfo):
    field: str | None = None


class ErrorResponse(BaseModel):
    error: FieldError


class RecipeConflict(ErrorResponse):
    layer: ClipLayer


class ValidationProblem(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class RequestError(BaseModel):
    detail: list[ValidationProblem]


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse | RequestError},
}
