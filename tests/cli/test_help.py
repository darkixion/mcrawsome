import sys
from collections.abc import Iterator

import click
import pytest
from click.testing import CliRunner

from mcrawsome.cli.main import cli


def _groups(group: click.Group, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], click.Group]]:
    yield path, group
    ctx = click.Context(group)
    for name in group.list_commands(ctx):
        command = group.get_command(ctx, name)
        if isinstance(command, click.Group):
            yield from _groups(command, (*path, name))


@pytest.mark.parametrize("columns", [80, 60])
@pytest.mark.parametrize(("path", "group"), list(_groups(cli)))
def test_group_help_keeps_every_summary(columns: int, path: tuple[str, ...], group: click.Group) -> None:
    # CliRunner forces 80 columns unless terminal_width is supplied too.
    result = CliRunner().invoke(cli, [*path, "--help"], env={"COLUMNS": str(columns)}, terminal_width=columns)
    assert result.exit_code == 0, result.output
    command_list = result.output.split("Commands:\n", 1)[1]
    assert all(not line.rstrip().endswith("...") for line in command_list.splitlines())
    joined = " ".join(command_list.split())
    for name, command in group.commands.items():
        if not command.hidden:
            summary = command.get_short_help_str(limit=sys.maxsize)
            assert f"{name} {summary}" in joined
    assert all(len(line) <= columns for line in result.output.splitlines())


@pytest.mark.parametrize("columns", [80, 60])
@pytest.mark.parametrize("explicit", [True, False])
def test_long_summaries_wrap_under_the_description(columns: int, explicit: bool) -> None:
    summary = "Describe the clip's colour, exposure, noise and stabilisation with suggestions for the export."
    group = type(cli)("example")
    group.add_command(click.Command("measure", short_help=summary if explicit else None, help=summary))
    group.add_command(click.Command("hidden", help="Do not show this command.", hidden=True))
    result = CliRunner().invoke(group, ["--help"], env={"COLUMNS": str(columns)}, terminal_width=columns)
    assert result.exit_code == 0, result.output
    lines = result.output.split("Commands:\n", 1)[1].splitlines()
    assert " ".join(" ".join(lines).split()) == f"measure {summary}"
    description_column = lines[0].index("Describe")
    assert len(lines) > 1
    assert all(len(line) - len(line.lstrip()) == description_column for line in lines[1:])
    assert all(len(line) <= columns for line in lines)
