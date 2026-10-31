"""Dispatch desktop commands directly and load processing commands only when requested."""

import click

from mcrawsome import __version__
from mcrawsome.cli.application import (
    file_manager_menu,
    gui,
    install_desktop_entry,
    mount_service,
    send_to_resolve,
    server,
    unmount_all,
)
from mcrawsome.cli.output import LazyHelpGroup

PROCESSING_COMMANDS = {
    "analyse": "Measure a clip and suggest export settings.",
    "calibrate": "Make a camera profile from a recording or Gyroflow.",
    "copy": "Copy recordings from a card or phone, verified.",
    "doctor": "Check FFmpeg, memory, disk space and settings.",
    "export": "Export CinemaDNG, OpenEXR or MCRAW copies.",
    "gyro": "Fit gyro sync and plan stabilisation for a clip.",
    "info": "Describe a clip and its recording settings.",
    "support-bundle": "Collect checks and metadata for a bug report.",
}


@click.group(cls=LazyHelpGroup, lazy_commands=PROCESSING_COMMANDS)
@click.version_option(__version__, prog_name="mcrawsome")
def cli() -> None:
    """Open-source tools for MotionCam Pro RAW video."""


for command in (
    gui,
    server,
    mount_service,
    send_to_resolve,
    unmount_all,
    file_manager_menu,
    install_desktop_entry,
):
    cli.add_command(command)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
