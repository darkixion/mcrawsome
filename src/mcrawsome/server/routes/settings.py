"""`GET` and `PUT /settings`: the interface's settings. A PUT takes effect at once, roots included."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError

from mcrawsome.server.settings import InterfaceSettings, SettingsUpdate, SettingsView, save_settings

router = APIRouter()


@router.get("/settings")
def get_settings(request: Request) -> SettingsView:
    settings: InterfaceSettings = request.app.state.settings
    return SettingsView(**settings.model_dump(), problem=request.app.state.settings_problem)


@router.put("/settings")
def put_settings(request: Request, update: SettingsUpdate) -> SettingsView:
    """Replace every setting. A folder it adds must exist (422, `field` naming it); a saved folder that
    has gone since (an unplugged drive) may stay."""
    saved: InterfaceSettings = request.app.state.settings
    for folder in update.folders:
        if folder not in saved.folders and not folder.is_dir():
            message = f"{folder} does not exist or is not a folder"
            error = {"type": "value_error", "loc": ("body", "folders"), "msg": message}
            raise RequestValidationError([error])
    settings = InterfaceSettings.model_validate(update.model_dump())
    save_settings(settings)
    request.app.state.settings = settings
    request.app.state.settings_problem = None
    return SettingsView(**settings.model_dump())
