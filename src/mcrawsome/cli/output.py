"""Output plumbing shared by the commands: the console, --json, error codes and --help-all."""

from __future__ import annotations

import importlib
import json
import sys
from typing import Any

import click
from click.formatting import wrap_text
from pydantic import BaseModel
from rich.console import Console

from mcrawsome import errors
from mcrawsome.errors import ErrorCode
from mcrawsome.results import ErrorInfo

console = Console()


class HelpGroup(click.Group):
    """A group whose command summaries wrap without losing words."""

    group_class = type

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        rows = []
        for name in self.list_commands(ctx):
            command = self.get_command(ctx, name)
            if command is not None and not command.hidden:
                rows.append((name, command.get_short_help_str(limit=sys.maxsize)))
        if not rows:
            return
        with formatter.section("Commands"):
            name_width = max(len(name) for name, _ in rows) + 2
            indent = " " * formatter.current_indent
            continuation = " " * (formatter.current_indent + name_width)
            width = max(1, formatter.width - len(continuation))
            for name, summary in rows:
                lines = wrap_text(summary, width).splitlines() or [""]
                formatter.write(f"{indent}{name:<{name_width}}{lines[0]}\n")
                for line in lines[1:]:
                    formatter.write(f"{continuation}{line}\n")


class LazyHelpGroup(HelpGroup):
    """Show command summaries without importing the processors which implement those commands."""

    def __init__(self, *args: Any, lazy_commands: dict[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lazy_commands = dict(lazy_commands or {})

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(set(super().list_commands(ctx)) | self.lazy_commands.keys())

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is None and cmd_name in self.lazy_commands:
            module = importlib.import_module("mcrawsome.cli.processing")
            command = module.cli.get_command(ctx, cmd_name)
            if command is not None:
                self.add_command(command)
        return command

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        summaries = HelpGroup(self.name)
        for name in self.list_commands(ctx):
            command = self.commands.get(name)
            if command is None:
                command = click.Command(name, short_help=self.lazy_commands[name])
            summaries.add_command(command)
        summaries.format_commands(ctx, formatter)


class CliError(click.ClickException):
    """A command-line failure with a code; --json prints it as {"error": {...}}."""

    def __init__(self, message: str, *, code: ErrorCode) -> None:
        super().__init__(message)
        self.code: ErrorCode = code


def error_code(exc: BaseException) -> ErrorCode:
    """The code for an exception from the export machinery."""
    if isinstance(exc, CliError):
        return exc.code
    return errors.error_code(exc)


def error_info(exc: BaseException, message: str | None = None) -> ErrorInfo:
    return ErrorInfo(code=error_code(exc), message=message if message is not None else str(exc))


def fail(exc: BaseException, message: str | None = None) -> CliError:
    """A CliError for `exc`, with its code, to raise."""
    return CliError(message if message is not None else str(exc), code=error_code(exc))


def emit(document: BaseModel | dict[str, Any]) -> None:
    """Print the one JSON document of a --json run."""
    data = document.model_dump(mode="json") if isinstance(document, BaseModel) else document
    click.echo(json.dumps(data, indent=2, default=str))


def sections_document(sections: list[tuple[str, list[tuple[str, str]]]]) -> dict[str, Any]:
    return {title: dict(rows) for title, rows in sections}


def _print_field_docs(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    from mcrawsome.recipe.load import field_docs

    for doc in field_docs():
        allowed = f": {doc.allowed}" if doc.allowed else ""
        click.echo(f"--{doc.name}  {doc.kind}{allowed} (default {doc.default})")
        if doc.description:
            click.echo(f"    {doc.description}")
    ctx.exit()


def help_all_option() -> click.Option:
    return click.Option(
        ["--help-all"],
        is_flag=True,
        is_eager=True,
        expose_value=False,
        callback=_print_field_docs,
        help="Show every Recipe setting with its type, allowed values and default, and exit",
    )


class JsonCommand(click.Command):
    """A command with --json: one JSON document on stdout, nothing else, and errors as {"error": {...}}."""

    def __init__(self, *args: Any, help_all: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.params.append(
            click.Option(["--json", "as_json"], is_flag=True, help="Print the result as one JSON document")
        )
        if help_all:
            self.params.append(help_all_option())

    def invoke(self, ctx: click.Context) -> Any:
        if not ctx.params.get("as_json"):
            return super().invoke(ctx)
        console.quiet = True  # notes and progress bars would corrupt the document
        try:
            return super().invoke(ctx)
        except CliError as exc:
            emit({"error": ErrorInfo(code=exc.code, message=exc.message).model_dump()})
            ctx.exit(1)
        finally:
            console.quiet = False
