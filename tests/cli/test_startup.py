"""Desktop launches and command discovery must not initialise unrelated output processors."""

import json
import subprocess
import sys
from pathlib import Path

import click
import pytest

from mcrawsome.cli.main import PROCESSING_COMMANDS, cli


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["--version"],
        ["gui", "--help"],
        ["server", "--help"],
        ["mount-service", "--help"],
    ],
)
def test_desktop_command_discovery_needs_no_image_processors(arguments: list[str]) -> None:
    code = """
import importlib.abc, sys
class NoProcessors(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'numpy', 'cv2', 'scipy', 'OpenEXR'} or fullname.startswith(
            ('mcrawsome.output', 'mcrawsome.photo', 'mcrawsome.pipeline', 'mcrawsome.recipe',
             'mcrawsome.cli.processing')
        ):
            raise AssertionError('unexpected processor import: ' + fullname)
sys.meta_path.insert(0, NoProcessors())
from click.testing import CliRunner
from mcrawsome.cli.main import cli
result = CliRunner().invoke(cli, __import__('json').loads(sys.argv[1]))
assert result.exit_code == 0, (result.output, repr(result.exception))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, json.dumps(arguments)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_gui_launch_reaches_window_dispatch_without_importing_renderers(tmp_path: Path) -> None:
    code = """
import importlib.abc, sys
class NoRenderers(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'OpenEXR' or fullname.startswith(
            ('mcrawsome.output', 'mcrawsome.photo', 'mcrawsome.pipeline', 'mcrawsome.cli.processing',
             'mcrawsome.doctor', 'mcrawsome.library.scan')
        ):
            raise AssertionError('unexpected renderer import: ' + fullname)
sys.meta_path.insert(0, NoRenderers())
from click.testing import CliRunner
from mcrawsome.cli.main import cli
import mcrawsome.gui
seen = []
mcrawsome.gui.launch = lambda port, folders, say, **options: seen.append((port, folders, options))
result = CliRunner().invoke(cli, ['gui', '--port', '18873', '--browser', '--', *sys.argv[1:]])
assert result.exit_code == 0, (result.output, repr(result.exception))
assert len(seen) == 1
assert seen[0][0] == 18873
assert seen[0][2]['browser']
assert list(map(str, seen[0][2]['clips'])) == sys.argv[1:]
assert seen[0][1] == []
"""
    clip = tmp_path / 'space %f "quote".mcraw'
    clip.touch()
    second = tmp_path / "second.mcraw"
    second.touch()
    result = subprocess.run(
        [sys.executable, "-c", code, str(clip), str(second)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_lazy_summaries_and_processing_commands_stay_in_sync() -> None:
    from mcrawsome.cli import processing

    ctx = click.Context(cli)
    for name, summary in PROCESSING_COMMANDS.items():
        command = cli.get_command(ctx, name)
        assert command is processing.cli.commands[name]
        assert command.short_help == summary


def test_server_import_does_not_load_photo_or_exr_processors() -> None:
    code = """
import importlib.abc, sys
class NoOutputOnlyProcessors(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'OpenEXR' or fullname.startswith(
            ('mcrawsome.photo', 'mcrawsome.output.exr', 'mcrawsome.cli.processing')
        ):
            raise AssertionError('unexpected output-only import: ' + fullname)
sys.meta_path.insert(0, NoOutputOnlyProcessors())
from mcrawsome.server.app import create_app
assert callable(create_app)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_nested_legacy_help_and_recipe_options_are_still_available() -> None:
    from click.testing import CliRunner

    for arguments, expected in ((["export", "--help-all"], "--develop.black_level"),):
        result = CliRunner().invoke(cli, arguments)
        assert result.exit_code == 0, result.output
        assert expected in result.output
