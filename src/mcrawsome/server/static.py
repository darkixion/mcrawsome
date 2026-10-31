"""The built interface (`scripts/build_web.py` puts it in `server/web/`), served at `/` under a strict
Content Security Policy: the page loads nothing from anywhere but this server."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from mcrawsome.brand import DESKTOP_AGENT

WEB = Path(__file__).parent / "web"
CSP = (
    "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
    "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; style-src 'self' 'unsafe-inline'"
)
NOT_BUILT = """<!doctype html>
<meta charset="utf-8">
<title>MCRAWsome</title>
<p>The interface is not built: run scripts/build_web.py</p>
"""


def _file(bundle: Path, name: str) -> Path:
    """The bundle's file `name`; 404 for anything else, a path climbing out of the bundle included."""
    root = bundle.resolve()
    try:
        path = (root / name).resolve()
    except ValueError:  # a name the file system cannot take, such as one with a null character
        raise StarletteHTTPException(404) from None
    if not path.is_relative_to(root) or not path.is_file():
        raise StarletteHTTPException(404)
    return path


def add_interface(app: FastAPI) -> None:
    """Serve the bundle at `/`, after the API's routes so that it never shadows them."""

    def headers(request: Request) -> dict[str, str]:
        policy = CSP
        if request.headers.get("user-agent") == DESKTOP_AGENT:
            # pywebview constructs the native bridge's functions in JavaScript.
            policy += "; script-src 'self' 'unsafe-eval'"
        return {"Content-Security-Policy": policy}

    @app.get("/", include_in_schema=False, response_model=None)
    def index(request: Request) -> FileResponse | HTMLResponse:
        if not (WEB / "index.html").is_file():
            return HTMLResponse(NOT_BUILT, headers={"Cache-Control": "no-cache"})
        return FileResponse(WEB / "index.html", headers=headers(request) | {"Cache-Control": "no-cache"})

    @app.get("/{name:path}", include_in_schema=False)
    def bundled(name: str, request: Request) -> FileResponse:
        path = _file(WEB, name)
        cache = "no-cache" if path.name == "index.html" else "max-age=31536000, immutable"
        return FileResponse(path, headers=headers(request) | {"Cache-Control": cache})
