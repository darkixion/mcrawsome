"""Colour choices for Resolve settings."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from mcrawsome.colour.encoding import LEGACY, LOG_GAMUTS
from mcrawsome.colour.spaces import GAMUTS
from mcrawsome.colour.transfer import LOGC3_EI, TRANSFERS


class ColourChoice(BaseModel):
    id: str
    name: str


class ColourPair(BaseModel):
    gamut: str
    transfer: str


class ColourChoices(BaseModel):
    gamuts: list[ColourChoice]
    transfers: list[ColourChoice]
    log_transfers: list[str]
    logc3_ei: list[int]
    legacy: dict[str, ColourPair]


def colour_router(api: str) -> APIRouter:
    router = APIRouter(prefix=api)

    @router.get("/colour", response_model=ColourChoices)
    def colour_choices() -> ColourChoices:
        return ColourChoices(
            gamuts=[
                ColourChoice(id=key, name=" / ".join(value.names) if value.names else value.name)
                for key, value in GAMUTS.items()
            ],
            transfers=[ColourChoice(id=key, name=value.name) for key, value in TRANSFERS.items()],
            log_transfers=list(LOG_GAMUTS),
            logc3_ei=list(LOGC3_EI),
            legacy={
                key: ColourPair(gamut=value.gamut, transfer=value.transfer) for key, value in LEGACY.items()
            },
        )

    return router
