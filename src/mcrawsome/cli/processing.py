"""Processing and diagnostic commands, loaded only when explicitly requested."""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import click
import tomli_w
from pydantic import ValidationError
from rich.markup import escape
from rich.progress import BarColumn, Progress, TaskID, TextColumn, TimeRemainingColumn
from rich.table import Table

from mcrawsome import __version__
from mcrawsome.analysis.run import analyse_report
from mcrawsome.camera.profile import CameraProfile, ProfileError, find_profile, save_profile
from mcrawsome.cli.output import (
    CliError,
    HelpGroup,
    JsonCommand,
    console,
    emit,
    error_info,
    fail,
    sections_document,
)
from mcrawsome.develop.white_balance import pick_white_balance
from mcrawsome.doctor import default_workers, run_checks
from mcrawsome.errors import McError
from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ContainerError
from mcrawsome.io.describe import FrameTiming, describe, describe_video, frame_timing, info_document
from mcrawsome.io.directlog import DirectLogError, camera_from_minf, is_video_input, probe_direct_log
from mcrawsome.motion.gyroflow import import_gyroflow_profile, lens_profile, synced_gcsv
from mcrawsome.output.chunks import WorkFolder
from mcrawsome.output.dng_export import export_dng
from mcrawsome.output.export import ExportError
from mcrawsome.output.exr_export import export_exr
from mcrawsome.output.ffmpeg import FfmpegError, find_ffmpeg
from mcrawsome.output.mcraw_export import export_mcraw
from mcrawsome.output.plan import KINDS, plan_export, resolve_recipe
from mcrawsome.pipeline.calibrate import (
    CalibrationError,
    default_name,
    read_recording,
    recording_match,
)
from mcrawsome.pipeline.calibrate import (
    calibrate as calibrate_recording,
)
from mcrawsome.pipeline.stabilise import MotionOutcome, StabiliseError, plan_raw_clip, plan_video
from mcrawsome.progress import Phase
from mcrawsome.recipe.load import (
    RecipeError,
    build_recipe,
    field_layer,
    recipe_fields,
    recipe_to_toml,
)
from mcrawsome.recipe.schema import Recipe
from mcrawsome.results import ClipResult, PlannedClip


@click.group(cls=HelpGroup)
@click.version_option(__version__, prog_name="mcrawsome")
def cli() -> None:
    """Open-source tools for MotionCam Pro RAW video."""


@cli.command(cls=JsonCommand, short_help="Describe a clip and its recording settings.")
@click.argument("clip", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--raw", is_flag=True, help="Print the metadata exactly as MotionCam stored it, as JSON")
def info(clip: Path, raw: bool, as_json: bool) -> None:
    """Describe a .mcraw clip: camera, lens, sensor, exposure, recording and MotionCam's settings."""
    if is_video_input(clip):
        try:
            sections = describe_video(clip)
            frames = probe_direct_log(find_ffmpeg()[1], clip).frame_count if as_json else 0
        except (DirectLogError, FfmpegError) as exc:
            raise fail(exc) from None
        _show_info(clip, frames, sections, as_json)
        return
    try:
        c = Clip(clip)
    except (ContainerError, ValidationError, ValueError) as exc:
        raise fail(exc, f"{clip.name}: cannot read the clip ({exc})") from None
    with c:
        if raw:
            click.echo(
                json.dumps({"container": c.raw_metadata, "first_frame": c.raw_frame_metadata(0)}, indent=1)
            )
            return
        sections = describe(c)
        frames = c.frame_count
        timing = frame_timing(c.timestamps_ns)
    _show_info(clip, frames, sections, as_json, timing)


def _show_info(
    clip: Path,
    frames: int,
    sections: list[tuple[str, list[tuple[str, str]]]],
    as_json: bool,
    timing: FrameTiming | None = None,
) -> None:
    if as_json:
        emit(info_document(clip, frames, sections, timing))
    else:
        _print_sections(sections)


def _print_sections(sections: list[tuple[str, list[tuple[str, str]]]]) -> None:
    for title, rows in sections:
        table = Table(title=title, title_justify="left", title_style="bold", show_header=False, box=None)
        for label, value in rows:
            table.add_row(label, value)
        console.print(table)
        console.print()


def _add_recipe_options(command: click.Command) -> click.Command:
    for name, field, annotation in recipe_fields():
        if "." not in name:  # top-level settings (e.g. look) have hand-written options
            continue
        option_type: Any = annotation if annotation in (int, float, bool) else str
        command.params.append(
            click.Option(
                [f"--{name}", name.replace(".", "__")],
                type=option_type,
                default=None,
                help=f"{field.description or ''}",
                show_default=False,
                hidden=field_layer(field) == 3,
            )
        )
    return command


WORKERS = click.option("--workers", type=click.IntRange(min=1), default=default_workers(), show_default=True)
THERMAL_LIMIT = click.option(
    "--thermal-limit",
    "thermal_limit",
    type=click.FloatRange(60, 105),
    default=None,
    metavar="DEGREES",
    help="Pause rendering while the CPU is this hot (60 to 105 C) until it has cooled by 10 degrees",
)


def _run_one(
    clip: Path,
    recipe: Recipe,
    target: Path,
    force: bool,
    workers: int,
    frame_range: tuple[int, int] | None = None,
    show_progress: bool = True,
    restart: bool = False,
    thermal_limit: float | None = None,
) -> ClipResult:
    run = partial(
        {"dng": export_dng, "exr": export_exr, "mcraw": export_mcraw}[recipe.output.kind],
        force=force,
        workers=workers,
        frame_range=frame_range,
        restart=restart,
        thermal_limit=thermal_limit,
    )
    if not show_progress:
        return run(clip, target, recipe).to_model(clip)
    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console,
    ) as bar:
        task = bar.add_task(clip.name, total=None)

        def on_progress(
            phase: Phase, done: int, total: int, bar: Progress = bar, task: TaskID = task
        ) -> None:
            bar.update(task, description=f"{clip.name} {phase}", completed=done, total=total or None)

        return run(clip, target, recipe, progress=on_progress).to_model(clip)


@cli.command(cls=JsonCommand, help_all=True, short_help="Export CinemaDNG, OpenEXR or MCRAW copies.")
@click.argument("clips", nargs=-1, required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--to",
    type=click.Choice(KINDS),
    default=None,
    help=(
        "What to make: dng (a CinemaDNG folder, the default), exr (an OpenEXR folder in scene linear), or "
        "mcraw (a lossless RAW copy or repair)"
    ),
)
@click.option("--start", default=None, help="Export from this time: 5s, 1:05 or 120f")
@click.option("--duration", default=None, help="Export this long: 3s, 0:10 or 72f (default: to the end)")
@click.option(
    "--recipe",
    "recipes",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="Recipe TOML file(s), applied in order",
)
@click.option("--set", "sets", multiple=True, metavar="KEY=VALUE", help="Override any Recipe setting")
@click.option(
    "--output",
    "output_path",
    default=None,
    help="Output file or folder (ending in / or without an extension)",
)
@click.option("--force", is_flag=True, help="Replace existing outputs")
@click.option("--restart", is_flag=True, help="Start an interrupted export again instead of resuming it")
@WORKERS
@THERMAL_LIMIT
@click.option("--dry-run", is_flag=True, help="Show the resolved Recipe and planned outputs, render nothing")
@click.option(
    "--pick-neutral",
    metavar="FRAME,X,Y,WIDTH,HEIGHT",
    help="Set clip-wide white balance from an output-pixel rectangle after RAW crop and orientation",
)
@click.option("--no-space-check", is_flag=True, help="Do not check that the disk has room for the outputs")
def export(
    clips: tuple[Path, ...],
    to: str | None,
    start: str | None,
    duration: str | None,
    recipes: tuple[Path, ...],
    sets: tuple[str, ...],
    output_path: str | None,
    force: bool,
    restart: bool,
    workers: int,
    thermal_limit: float | None,
    dry_run: bool,
    pick_neutral: str | None,
    no_space_check: bool,
    as_json: bool,
    **fields: Any,
) -> None:
    """Export clips as CinemaDNG or OpenEXR folders for other software, or as MCRAW copies (trimmed or
    repaired). MCRAWsome does not encode video; send clips to Resolve from the interface instead.

    An interrupted export keeps its finished part beside the output: the same command resumes it. With
    The batch is planned and checked before processing begins."""
    try:
        plan = plan_export(
            clips, recipes, to, None, None, start, duration, fields, sets, output_path,
            force, dry_run, space_check=not no_space_check,
        )  # fmt: skip
        if pick_neutral is not None:
            try:
                coordinates = tuple(int(part) for part in pick_neutral.split(","))
                if len(coordinates) != 5:
                    raise ValueError("Use --pick-neutral FRAME,X,Y,WIDTH,HEIGHT with five whole numbers")
                frame, x, y, width, height = coordinates
                picked = []
                for planned in plan:
                    kelvin, tint = pick_white_balance(
                        planned.clip, planned.recipe, frame, (x, y, width, height)
                    )
                    custom = {"white_balance": "custom", "temperature": kelvin, "tint": tint}
                    recipe = build_recipe([planned.recipe.model_dump(), {"develop": custom}])
                    picked.append(dataclasses.replace(planned, recipe=recipe))
                plan = picked
            except (ValueError, RecipeError) as exc:
                raise McError(str(exc), code="recipe-invalid") from None
    except McError as exc:
        raise fail(exc) from None
    missing = [clip for clip in clips if not clip.exists()]
    for planned in plan:
        for note in planned.notes:
            console.print(f"[yellow]Note:[/yellow] {escape(note)}", soft_wrap=True)

    if dry_run:
        for planned in plan:
            for note in planned.ignored:
                console.print(f"[yellow]Note:[/yellow] {escape(note)}", soft_wrap=True)
        for planned in plan:
            state = " (missing)" if planned.clip in missing else ""
            console.print(
                f"{planned.clip}{state} -> {planned.target}", markup=False, highlight=False, soft_wrap=True
            )
            console.print(recipe_to_toml(planned.recipe), markup=False, highlight=False, soft_wrap=True)
        if as_json:
            documents = [
                PlannedClip(
                    clip=p.clip,
                    output=p.target,
                    missing=p.clip in missing,
                    recipe=p.recipe.model_dump(mode="json", exclude_none=True),
                )
                for p in plan
            ]
            emit({"results": [d.model_dump(mode="json") for d in documents]})
            sys.exit(1 if missing else 0)
        if missing:
            raise CliError(
                f"{len(missing)} input(s) missing: {', '.join(c.name for c in missing)}",
                code="input-missing",
            )
        return
    results: list[ClipResult] = []
    for planned in plan:
        clip, recipe, target, frame_range = planned.clip, planned.recipe, planned.target, planned.frame_range
        try:
            result = _run_one(
                clip, recipe, target, force, workers, frame_range, show_progress=not as_json, restart=restart,
                thermal_limit=thermal_limit,
            )  # fmt: skip
            console.print(
                f"[green]Done:[/green] {escape(str(result.output))} ({result.frames} frames)", soft_wrap=True
            )
            for warning in result.notes:
                console.print(f"[yellow]Note:[/yellow] {escape(warning)}", soft_wrap=True)
            result.notes[:0] = planned.notes  # already printed at planning; the document carries them too
        except (McError, FfmpegError, OSError) as exc:  # this clip failed: the batch carries on
            info = error_info(exc)
            console.print(
                f"[red]Failed \\[{info.code}]:[/red] {escape(clip.name)}: {escape(info.message)}",
                soft_wrap=True,
            )
            result = ClipResult(clip=clip, notes=list(planned.notes), error=info)
        except KeyboardInterrupt:  # the workers are stopped; the work folder stays
            if recipe.output.kind == "mcraw":
                then = "an MCRAW copy starts again; run the same command again"
            elif WorkFolder(target).load() is not None:
                then = "run the same command again to resume"
            else:
                then = "nothing was kept; the same command starts again"
            console.print(f"[yellow]Interrupted:[/yellow] {escape(clip.name)}: {then}", soft_wrap=True)
            sys.exit(130)
        results.append(result)
    if as_json:
        emit({"results": [r.model_dump(mode="json") for r in results]})
    sys.exit(1 if any(r.error for r in results) else 0)


@cli.command(cls=JsonCommand, short_help="Check FFmpeg, memory, disk space and settings.")
@click.option("--refresh", is_flag=True, help="Try the GPU decoders again instead of using the cache")
def doctor(refresh: bool, as_json: bool) -> None:
    """Check this machine: FFmpeg and its LOG decoders, the native modules, memory, disk space and settings.

    Exits 1 when a check fails; a warning only means something is optional or reduced."""
    checks = run_checks(refresh=refresh)
    failed = any(c.status == "fail" for c in checks)
    if as_json:
        emit({"checks": [c.model_dump(mode="json") for c in checks]})
        sys.exit(1 if failed else 0)
    table = Table(show_header=True, header_style="bold")
    for column in ("", "Check", "Result"):
        table.add_column(column, overflow="fold")
    marks = {"ok": "[green]ok[/green]", "warn": "[yellow]warn[/yellow]", "fail": "[red]FAIL[/red]"}
    for c in checks:
        detail = escape(c.detail) + (
            f"\n[dim]Fix: {escape(c.fix)}[/dim]" if c.fix and c.status != "ok" else ""
        )
        table.add_row(marks[c.status], escape(c.name), detail)
    console.print(table)
    sys.exit(1 if failed else 0)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


@cli.command("copy", cls=JsonCommand, short_help="Copy recordings from a card or phone, verified.")
@click.argument("sources", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@click.argument("destination", type=click.Path(file_okay=False, path_type=Path))
@click.option("--no-verify", is_flag=True, help="Do not read each copy back to compare it with the source")
def copy_recordings(sources: tuple[Path, ...], destination: Path, no_verify: bool, as_json: bool) -> None:
    """Copy MotionCam recordings (SOURCE: a folder, searched with its subfolders, or a .mcraw) into
    DESTINATION, with their split parts and the files named after them (.gcsv, .json, .mcrawsome.toml).

    Each file is hashed as it is copied, read back from the disk and compared before it takes its name, so
    a card can be cleared afterwards. Files already copied are skipped; a different file of the same name is
    never replaced. A copy stopped part-way leaves a .part file that the next copy continues."""
    from mcrawsome.library.offload import CopyProgress, find_recordings, offload_recordings

    try:
        recordings = find_recordings(sources)
    except McError as exc:
        raise fail(exc) from None
    if not recordings:
        raise CliError("No MotionCam recordings (.mcraw) were found", code="input-missing")
    total = sum(r.size for r in recordings)
    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.fields[speed]}"),
        TimeRemainingColumn(),
        console=console,
        disable=as_json,
    ) as bar:
        task = bar.add_task("Copying", total=total, speed="")

        def on_progress(p: CopyProgress) -> None:
            speed = f"{p.bytes_per_s / 1e6:.0f} MB/s" if p.bytes_per_s else ""
            bar.update(
                task, description=f"{p.phase.capitalize()} {p.file}", completed=p.done_bytes, speed=speed
            )

        try:
            results = offload_recordings(recordings, destination, verify=not no_verify, progress=on_progress)
        except McError as exc:
            raise fail(exc) from None
    failed = [r for r in results if r.status == "failed"]
    if as_json:
        emit(
            {
                "recordings": len(recordings),
                "bytes": total,
                "files": [r.model_dump(mode="json") for r in results],
            }
        )
        sys.exit(1 if failed else 0)
    counts = {status: sum(r.status == status for r in results) for status in ("copied", "skipped", "failed")}
    console.print(
        f"{_count(len(recordings), 'recording')} to {escape(str(destination))}: "
        f"{_count(counts['copied'], 'file')} copied"
        + (" and verified" if not no_verify else "")
        + f", {counts['skipped']} already copied, {counts['failed']} failed",
        soft_wrap=True,
    )
    for result in failed:
        console.print(f"[red]Failed[/red] {escape(result.message)}", soft_wrap=True, highlight=False)
    sys.exit(1 if failed else 0)


@cli.command("support-bundle", short_help="Collect checks and metadata for a bug report.")
@click.argument("items", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--output",
    "out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="The zip to write (default: mcrawsome-support-YYYYmmdd-HHMMSS.zip in the current folder)",
)
@click.option("--force", is_flag=True, help="Replace an existing bundle")
def support_bundle(items: tuple[Path, ...], out: Path | None, force: bool) -> None:
    """Collect what a bug report needs into one zip: doctor's checks, the system, and the
    sidecars and clip metadata given (SIDECAR_OR_CLIP...). It holds no picture or sound."""
    from mcrawsome.support import make_bundle  # loaded only for this command

    if out is None:
        out = Path(f"mcrawsome-support-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip")
    if out.exists() and not force:
        raise CliError(f"{out} already exists (use --force to replace it)", code="output-exists")
    names = make_bundle(out, list(items))
    console.print(f"[green]Wrote[/green] {escape(str(out))}:", soft_wrap=True)
    for name in names:
        console.print(f"  {escape(name)}", soft_wrap=True, highlight=False)
    console.print(
        "Your home folder is written as ~, but file and folder names are kept: look through it before "
        "sharing.",
        soft_wrap=True,
    )


_AXIS_NAMES = "XYZ"


def _axes_text(axes: list[int], signs: list[int]) -> str:
    parts = [
        f"{what} from {'+' if sign > 0 else '-'}{_AXIS_NAMES[axis]}"
        for what, axis, sign in zip(("image x", "image y", "roll"), axes, signs, strict=True)
    ]
    return ", ".join(parts)


def _fit_rows(m: dict[str, Any]) -> list[tuple[str, str]]:
    rows = [("Profile", str(m["camera_profile"]))]
    if "sync_offset_ms" not in m:
        return rows
    constrained = " (axes from the profile)" if m["sync_constrained"] else ""
    rows += [
        ("Sync offset", f"{m['sync_offset_ms']:+.1f} ms"),
        ("Axes", _axes_text(m["gyro_axes"], m["gyro_signs"]) + constrained),
        ("Focal length", f"{m['focal_px']:.0f} px"),
        ("Readout", f"{m['readout_ms']:.1f} ms"),
        ("Confidence", f"the gyro explains {max(m['sync_confidence'], 0.0):.0%} of the motion"),
    ]
    if m["stabilised"]:
        rows += [
            ("Correction", f"mean {m['mean_correction_deg']:.2f} deg, max {m['max_correction_deg']:.2f} deg"),
            ("Reduced smoothing", f"{m['reduced_smoothing_frames']} frames"),
            ("Coverage", f"{m['gyro_coverage']:.0%} of the frames"),
        ]
    return rows


def _motion(clip: Path, recipe: Recipe, workers: int, staging: Path) -> tuple[MotionOutcome, str, str]:
    """The export's motion step, with the camera's brand and model for Gyroflow."""
    if is_video_input(clip):
        ffmpeg, ffprobe = find_ffmpeg()
        info = probe_direct_log(ffprobe, clip)
        outcome, device = plan_video(ffmpeg, info, recipe, workers, staging)
        model, _ = camera_from_minf(device.get("MINF", ""))
        return outcome, "", model or ""
    outcome = plan_raw_clip(clip, recipe, workers, staging)
    with Clip(clip) as c:
        return outcome, c.metadata.manufacturer or "", c.metadata.device_model or ""


@cli.command(cls=JsonCommand, help_all=True, short_help="Measure a clip and suggest export settings.")
@click.argument("clip", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--recipe",
    "recipes",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="Recipe TOML file(s)",
)
@click.option("--set", "sets", multiple=True, metavar="KEY=VALUE", help="Override any Recipe setting")
@WORKERS
def analyse(
    clip: Path,
    recipes: tuple[Path, ...],
    sets: tuple[str, ...],
    workers: int,
    as_json: bool,
) -> None:
    """Measure a clip and report exposure, clipping, noise, white balance, brightness and stabilisation,
    with suggestions. Renders nothing."""
    try:
        recipe = resolve_recipe(clip, recipes, None, {}, sets)
    except RecipeError as exc:
        raise fail(exc, f"{clip.name}: {exc}") from None
    try:
        from mcrawsome.paths import cache_dir

        report = analyse_report(clip, recipe, workers, cache_dir() / "analysis" / "work")
    except (ExportError, StabiliseError, DirectLogError, FfmpegError, ContainerError, ValueError) as exc:
        raise fail(exc, f"{clip.name}: {exc}") from None
    if as_json:
        emit(
            {"clip": str(clip)}
            | sections_document(report.sections)
            | {"warnings": report.warnings, "suggestions": report.suggestions}
        )
        return
    _print_sections(report.sections)
    for warning in report.warnings:
        console.print(f"[yellow]Note:[/yellow] {escape(warning)}", soft_wrap=True)
    for suggestion in report.suggestions:
        console.print(f"[cyan]Suggestion:[/cyan] {escape(suggestion)}", soft_wrap=True)


@cli.command(short_help="Fit gyro sync and plan stabilisation for a clip.")
@click.argument("clip", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--gyroflow",
    "folder",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Write a Gyroflow lens profile (and, for RAW clips, a synced .gcsv) to this folder",
)
@click.option("--set", "sets", multiple=True, metavar="KEY=VALUE", help="Override any Recipe setting")
@click.option("--force", is_flag=True, help="Replace existing Gyroflow files")
@WORKERS
def gyro(clip: Path, folder: Path | None, sets: tuple[str, ...], force: bool, workers: int) -> None:
    """Fit the gyro sync and plan the stabilisation of a clip, without rendering, and report them."""
    try:
        recipe = resolve_recipe(clip, (), None, {}, sets, {"stabilise": "auto"})
    except RecipeError as exc:
        raise click.ClickException(f"{clip.name}: {exc}") from None
    try:
        with tempfile.TemporaryDirectory(prefix="mcrawsome-gyro-") as staging:
            outcome, brand, model = _motion(clip, recipe, workers, Path(staging))
    except (StabiliseError, DirectLogError, FfmpegError, ContainerError, ValidationError, ValueError) as exc:
        raise click.ClickException(f"{clip.name}: {exc}") from None
    _print_sections([(clip.name, _fit_rows(outcome.measurements))])
    for note in outcome.notes:
        console.print(f"[yellow]Note:[/yellow] {escape(note)}", soft_wrap=True)
    if folder is not None:
        _write_gyroflow(clip, outcome, folder, force, brand, model)


def _write_gyroflow(
    clip: Path, outcome: MotionOutcome, folder: Path, force: bool, brand: str, model: str
) -> None:
    fitted = outcome.gyro
    if fitted is None:
        raise click.ClickException(f"{clip.name}: no gyro fit, so there is nothing to write for Gyroflow")
    trusted = fitted.fit.confident
    profile = fitted.profile
    if trusted:
        profile = profile.model_copy(update={"focal_px": fitted.fit.focal_px})
    lens_path = folder / f"{clip.stem}_lens.json"
    gcsv_path = folder / f"{clip.stem}_synced.gcsv"
    targets = [lens_path] + ([gcsv_path] if trusted and fitted.log.clock == "epoch" else [])
    existing = [p for p in targets if p.exists()]
    if existing and not force:
        raise click.ClickException(f"{existing[0]} already exists (use --force to replace it)")
    try:
        data = lens_profile(profile, fitted.size, fitted.fps, camera_brand=brand, camera_model=model)
    except ProfileError as exc:
        raise click.ClickException(str(exc)) from None
    folder.mkdir(parents=True, exist_ok=True)
    lens_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if gcsv_path in targets:
        gcsv_path.write_text(synced_gcsv(fitted.log, fitted.first_centre_s), encoding="utf-8")
    for path in targets:
        console.print(f"[green]Wrote[/green] {escape(str(path))}", soft_wrap=True)
    if not trusted:
        console.print(
            "[yellow]Note:[/yellow] the sync is not trusted, so the lens profile uses the profile's or the "
            "metadata's focal length and no synced .gcsv was written",
            soft_wrap=True,
        )


@cli.command(short_help="Make a camera profile from a recording or Gyroflow.")
@click.argument("clip", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--save", is_flag=True, help="Save the profile to your camera folder (~/.config/mcrawsome/cameras)"
)
@click.option("--name", default=None, help="Profile name (lower-case letters, digits and underscores)")
@click.option(
    "--from-gyroflow",
    "lens_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Import a Gyroflow lens calibration, focal length and readout instead of measuring them",
)
@click.option("--force", is_flag=True, help="Replace a saved profile of the same name")
@WORKERS
def calibrate(
    clip: Path, save: bool, name: str | None, lens_file: Path | None, force: bool, workers: int
) -> None:
    """Make a camera profile from a calibration recording, or from a Gyroflow lens profile.

    Record a static, distant, detailed scene with slow pans, tilts and rolls in every direction. The
    profile matches this camera at this resolution; axes and sync are fitted per clip either way.
    """
    reason: str | None = None
    notes: list[str] = []
    try:
        recording = read_recording(clip, workers)
        if lens_file is not None:
            try:
                data = json.loads(lens_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise click.ClickException(f"{lens_file.name}: cannot read it ({exc})") from None
            imported = import_gyroflow_profile(data, recording.width, recording.height)
            if imported.lens_calibration is not None:
                notes.append(
                    "the measured lens geometry is retained for Gyroflow interchange; "
                    "MCRAWsome does not yet apply lens correction to image pixels"
                )
                try:
                    imported.lens_calibration.matrix_at(recording.width, recording.height)
                except ValueError as exc:
                    notes.append(f"{exc}; use a calibration for this recording mode before exporting it")
            # a lens profile has no axes or sync: keep those of the profile this one will replace
            current = find_profile(
                recording.device_model, recording.camera_id, recording.width, recording.height, recording.fps
            )
            kept = (
                current.model_dump(
                    include={
                        "gcsv_axes",
                        "gpmf_axes",
                        "gcsv_sync_ms",
                        "gpmf_sync_ms",
                        "gcsv_gravity_axes",
                        "gcsv_gravity_camera_ids",
                    }
                )
                if current
                else {}
            )
            profile = CameraProfile.model_validate(
                imported.model_dump()
                | kept
                | {"name": name or default_name(recording), "match": recording_match(recording)}
            )
            confidence = None
        else:
            result = calibrate_recording(recording, name)
            profile, reason, notes = result.profile, result.fit.reason, result.notes
            confidence = result.fit.confidence
    except ValidationError:
        raise click.ClickException("--name: use lower-case letters, digits and underscores") from None
    except (CalibrationError, ProfileError, DirectLogError, FfmpegError, ContainerError, ValueError) as exc:
        raise click.ClickException(f"{clip.name}: {exc}") from None
    console.print(f"[bold]{escape(profile.name)}[/bold]")
    console.print(tomli_w.dumps(profile.model_dump(mode="json", exclude_none=True)), markup=False)
    if confidence is not None:
        verdict = "" if reason is None else f"; not trusted: {reason}"
        console.print(
            f"Confidence: the gyro explains {max(confidence, 0.0):.0%} of the motion{verdict}",
            markup=False,
            soft_wrap=True,
        )
    for note in notes:
        console.print(f"[yellow]Note:[/yellow] {escape(note)}", soft_wrap=True)
    if not save:
        console.print("Add --save to keep this profile.")
        return
    if reason is not None:
        raise click.ClickException(f"not saved: {reason}")
    try:
        path = save_profile(profile, force=force)
    except ProfileError as exc:
        raise click.ClickException(str(exc)) from None
    console.print(f"[green]Saved[/green] {escape(str(path))}", soft_wrap=True)


_add_recipe_options(export)
