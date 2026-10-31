"""Desktop and engine entry commands, without importing output processors for registration/help."""

from __future__ import annotations

import subprocess
from functools import partial
from pathlib import Path

import click

from mcrawsome.cli.output import CliError, console, fail
from mcrawsome.errors import McError


@click.command("mount-service", hidden=True)
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
def mount_service(folder: Path) -> None:
    """Serve one mounted sequence until it is unmounted (the engine starts this)."""
    from mcrawsome.mount.service import serve

    serve(folder)


@click.command(short_help="Serve the local media HTTP API.")
@click.option("--host", default=None, help="The address to listen on (default 127.0.0.1: this machine only)")
@click.option("--port", type=click.IntRange(1, 65535), default=None, help="The port (default 8642)")
@click.option(
    "--token",
    envvar="MCRAWSOME_SERVER_TOKEN",
    default=None,
    help="Require 'Authorization: Bearer TOKEN' on every request; needed to listen on another address",
)
@click.option(
    "--library",
    "roots",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="A folder whose recordings the API may read (default: the current folder)",
)
@click.option("--workers", type=click.IntRange(min=1), default=None, help="Workers for clip analysis")
def server(
    host: str | None,
    port: int | None,
    token: str | None,
    roots: tuple[Path, ...],
    workers: int | None,
) -> None:
    """Serve the media API using server.toml settings and command options."""
    from mcrawsome.server.app import API, create_app, serve
    from mcrawsome.server.config import UNENCRYPTED, is_loopback, load_config

    options = {
        "host": host, "port": port, "token": token, "roots": [root.absolute() for root in roots],
        "workers": workers,
    }  # fmt: skip
    try:
        config = load_config(options)
        app = create_app(config)
    except McError as exc:
        raise fail(exc) from None
    address = f"[{config.host}]" if ":" in config.host else config.host
    console.print(f"Serving the media API at http://{address}:{config.port}{API}", soft_wrap=True)
    if not is_loopback(config.host):
        console.print(f"[yellow]Note:[/yellow] {UNENCRYPTED}.", soft_wrap=True)
    serve(app, config)


@click.command(short_help="Open a window on the local engine.")
@click.argument("clips", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--port", type=click.IntRange(1, 65535), default=None, help="The server's port (default 8642)")
@click.option(
    "--library",
    "roots",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="For a server started here: a library folder",
)
@click.option("--browser", is_flag=True, help="Open the interface in the default browser")
@click.option("--view", type=click.Choice(["library", "settings"]), default=None, help="Show this screen")
def gui(
    clips: tuple[Path, ...], port: int | None, roots: tuple[Path, ...], browser: bool, view: str | None
) -> None:
    """Open a window on the local engine, starting it when needed."""
    from mcrawsome.gui import launch
    from mcrawsome.server.config import load_config

    try:
        config = load_config({"port": port})
        folders = [root.absolute() for root in roots]
        say = partial(console.print, soft_wrap=True)
        launch(config.port, folders, say, browser=browser, clips=clips, view=view)
    except McError as exc:
        raise fail(exc) from None
    except RuntimeError as exc:
        raise CliError(str(exc), code="internal-error") from None


@click.command("desktop-entry", short_help="Add this AppImage to your application menu.")
@click.option("--remove", is_flag=True, help="Remove the menu entry, icon and .mcraw file registration")
def install_desktop_entry(remove: bool) -> None:
    """Add this AppImage to your application menu and register .mcraw files, for this user only.

    Existing default applications are preserved. Run again after moving or replacing the AppImage.
    """
    from mcrawsome.desktop import desktop_entry

    try:
        path = desktop_entry(remove)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"{'Removed' if remove else 'Wrote'} {path}")


@click.command("unmount-all", short_help="Unmount every recording mounted for Resolve.")
@click.option("--port", type=click.IntRange(1, 65535), default=None, help="The server's port (default 8642)")
@click.option("--notify", "announce", is_flag=True, help="Also show the outcome as a desktop notification")
def unmount_all(port: int | None, announce: bool) -> None:
    """Unmount all of MCRAWsome's media the normal way. Media an application such as DaVinci Resolve still
    has open stays mounted, and is named."""
    import urllib.error

    from mcrawsome.gui import answers, request
    from mcrawsome.notifications import notify
    from mcrawsome.server.config import load_config

    engine = load_config({"port": port}).port
    try:
        running = answers(engine)
        results = request(engine, "/mounts/unmount-all", "POST", timeout=60)["results"] if running else []
    except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        raise CliError(f"Could not reach MCRAWsome's engine: {exc}", code="engine-busy") from None
    kept = [item for item in results if not item["ok"]]
    recordings = f"recording{'s' if len(results) != 1 else ''}"
    if not results:
        summary, body = "Nothing to unmount", "No recordings are mounted."
    elif not kept:
        summary, body = f"Unmounted {len(results)} {recordings}", "DaVinci Resolve can no longer read them."
    else:
        summary = f"{len(kept)} of {len(results)} {recordings} still mounted"
        body = "; ".join(f"{Path(item['source']).name}: {item['message']}" for item in kept)
    if announce:
        notify(summary, body)
    if kept:
        raise CliError(f"{summary}. {body}", code="engine-busy")
    click.echo(f"{summary}. {body}")


@click.command("send-to-resolve", short_help="Send RAW recordings to Resolve without opening a window.")
@click.argument(
    "clips", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--mode", type=click.Choice(["bin", "timeline"]), default="bin", show_default=True)
@click.option("--port", type=click.IntRange(1, 65535), default=None)
def send_to_resolve(clips: tuple[Path, ...], mode: str, port: int | None) -> None:
    """Prepare virtual media with each clip's saved Resolve settings, then open its native archive.

    The engine and mounts remain available for Resolve. Existing engines and other mounts are kept.
    """
    from typing import Literal, cast

    from mcrawsome.file_actions import send_files
    from mcrawsome.server.config import load_config

    try:
        send_files(
            load_config({"port": port}).port, clips, cast(Literal["bin", "timeline"], mode), click.echo
        )
    except McError as exc:
        raise fail(exc) from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from None


@click.command(
    "file-manager-menu", short_help="Enable or disable the file-manager Resolve menu for your account."
)
@click.option("--remove", is_flag=True, help="Disable MCRAWsome's menu for your account")
@click.option(
    "--install-support",
    is_flag=True,
    help="Ask for administrator authentication to install missing Nautilus support",
)
def file_manager_menu(remove: bool, install_support: bool) -> None:
    """Save the personal preference for the packaged context-menu adapter."""
    from mcrawsome.desktop_actions import set_menu

    try:
        status = set_menu(not remove, install_support=install_support and not remove)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(status["message"] if not remove else "Disabled MCRAWsome's file-manager menu")
