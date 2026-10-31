import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from mcrawsome.mount.manager import MountedSequence
from mcrawsome.resolve import native


class Launcher:
    def __init__(self, code: int = 0, running: bool = False) -> None:
        self.code, self.running = code, running
        self.waits: list[float | None] = []
        self.reaped = threading.Event()

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.running and timeout is not None:
            raise subprocess.TimeoutExpired("xdg-open", timeout)
        self.reaped.set()
        return self.code


def test_desktop_activation_passes_the_exact_archive_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "clip with spaces;literal.drb"
    calls: list[tuple[list[str], dict[str, Any]]] = []
    process = Launcher()

    def launch(command: list[str], **kwargs: Any) -> Launcher:
        calls.append((command, kwargs))
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    native.open_archive(path)
    command, options = calls[0]
    assert command == ["xdg-open", str(path)]
    assert "env" not in options
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stdout"] == subprocess.DEVNULL
    assert options["stderr"] == subprocess.DEVNULL
    assert options["start_new_session"] is True
    assert process.reaped.is_set()


def test_desktop_failure_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: Launcher(code=3))
    with pytest.raises(OSError, match="exit status 3"):
        native.open_archive(tmp_path / "clip.drb")


def test_a_running_handler_is_a_launched_send_and_is_reaped_without_being_killed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = Launcher(running=True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: process)

    def block(*a: Any, **kw: Any) -> None:
        raise subprocess.TimeoutExpired("xdg-open", 15)

    monkeypatch.setattr(subprocess, "run", block)
    monkeypatch.setattr(native, "write_timeline", lambda path, mounts, **kw: path.write_bytes(b"test"))
    mounts = [
        MountedSequence(
            id=str(i), source=tmp_path / f"clip{i}.mcraw", path=tmp_path / str(i), stamp=(1, 2, 3, 4)
        )
        for i in range(2)
    ]
    monkeypatch.setattr(
        native, "timeline_indices", lambda mounts, rotations: [(list(range(len(mounts))), None)]
    )
    result = native.send_mounts(mounts, "auto", mode="timeline")
    assert result.launched
    assert result.archive is not None
    assert result.archive.suffix == ".drt"
    assert [item.id for item in result.results] == ["mount-0", "mount-1"]
    assert process.reaped.wait(1)
    assert len(process.waits) == 2
    assert process.waits[1] is None
    assert process.waits[0] is not None
    assert process.waits[0] <= 1


def test_retention_is_saved_after_archive_creation_and_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def write(path: Path, *_: Any, **kwargs: Any) -> None:
        path.write_bytes(b"synthetic archive")
        events.append("archive")

    monkeypatch.setattr(native, "write_bin", write)
    monkeypatch.setattr(native, "open_archive", lambda _: events.append("dispatch"))
    result = native.send_mounts([], "auto", on_archive=lambda: events.append("retain"))
    assert result.launched
    assert events == ["archive", "retain", "dispatch"]
    events.clear()

    def refused() -> None:
        raise OSError("Could not persist sent-media record")

    result = native.send_mounts([], "auto", on_archive=refused)
    assert not result.launched
    assert result.archive is not None
    assert result.archive.exists()
    assert events == ["archive"]


def test_gyro_uses_native_timeline_and_retains_provenance_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def write(path: Path, mounts: list[Any], **kwargs: Any) -> int:
        calls.append(kwargs)
        path.write_bytes(b"archive")
        return 1

    monkeypatch.setattr(native, "write_timeline", write)
    monkeypatch.setattr(native, "open_archive", lambda _: None)
    mount = MountedSequence(id="a", source=tmp_path / "source.mcraw", path=tmp_path, stamp=(1, 2, 3, 4))
    result = native.send_mounts([mount], "auto", mode="timeline")
    assert result.launched
    assert calls[0]["stabilise"] is True
    assert calls[0]["full_resolution"] is False
    assert "unchecked" not in str(result.archive)
    assert "bypassed" not in result.results[0].message
    assert "Stabilize" in result.results[0].message
    assert ".drt.gyro" in result.results[0].message
    assert "1 of 1 clips are stabilised" in result.results[0].message


def test_gyro_timelines_refuse_proxy_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    monkeypatch.setattr(native, "open_archive", lambda _: pytest.fail("opened a refused send"))
    mount = MountedSequence(
        id="0" * 24, source=tmp_path / "clip.mcraw", path=tmp_path / "media", stamp=(1, 2, 3, 4),
        raw_settings=RawCalibration(mount_proxy="half"),
    )  # fmt: skip
    (tmp_path / "clip.gcsv").write_text("gyro")  # so the clip is to be stabilised
    result = native.send_mounts([mount], "auto", mode="timeline")
    assert not result.launched
    assert "full-resolution" in result.results[0].message


def test_gyro_timelines_refuse_cropped_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    monkeypatch.setattr(native, "open_archive", lambda _: pytest.fail("opened a refused send"))
    mount = MountedSequence(
        id="0" * 24, source=tmp_path / "clip.mcraw", path=tmp_path / "media", stamp=(1, 2, 3, 4),
        raw_settings=RawCalibration(mount_crop="auto"),
    )  # fmt: skip
    (tmp_path / "clip.gcsv").write_text("gyro")  # so the clip is to be stabilised
    result = native.send_mounts([mount], "auto", mode="timeline")
    assert not result.launched
    assert "uncropped" in result.results[0].message


def test_sends_separate_timelines_per_displayed_resolution_and_maps_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    mounts = [
        MountedSequence(
            id=str(i), source=tmp_path / f"clip-{i}.mcraw", path=tmp_path / str(i), stamp=(1, 2, 3, 4)
        )
        for i in range(3)
    ]
    dimensions = {"0": (4096, 2304), "1": (3840, 2160), "2": (2304, 4096)}
    monkeypatch.setattr(
        native,
        "mounted_media",
        lambda mount: SimpleNamespace(
            rate=24.0, info=SimpleNamespace(width=dimensions[mount.id][0], height=dimensions[mount.id][1])
        ),
    )
    built = []
    opened: list[Path] = []

    def write(path: Path, grouped: list[MountedSequence], **kwargs: Any) -> int:
        built.append(([mount.id for mount in grouped], kwargs.get("rotations")))
        path.write_bytes(b"timeline")
        return 0

    monkeypatch.setattr(native, "write_timeline", write)
    monkeypatch.setattr(native, "open_archive", opened.append)
    result = native.send_mounts(mounts, "auto", mode="timeline", rotations=[0, 0, 90])
    assert result.launched
    assert built == [(["0", "2"], [0, 90]), (["1"], [0])]
    assert len(result.archives) == len(opened) == 2
    assert "4096x2304" in result.archives[0].name
    assert "3840x2160" in result.archives[1].name
    assert result.results[0].archive == result.results[2].archive == result.archives[0]
    assert result.results[1].archive == result.archives[1]
    assert all(item.dispatched for item in result.results)


@pytest.mark.parametrize(
    ("sizes", "groups", "labels", "says"),
    [
        (
            [(3840, 2160)] * 4,
            [["0", "2"], ["1", "3"]],
            [" 24 fps", " 25 fps"],
            "Sent 2 timelines, one for each frame rate.",
        ),
        (
            [(3840, 2160)] * 3 + [(1920, 1080)],
            [["0", "2"], ["1"], ["3"]],
            [" 24 fps 3840x2160", " 25 fps 3840x2160", " 25 fps 1920x1080"],
            "Sent 3 timelines, one for each frame rate and displayed resolution.",
        ),
    ],
)
def test_sends_separate_timelines_per_frame_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sizes: list[tuple[int, int]],
    groups: list[list[str]],
    labels: list[str],
    says: str,
) -> None:
    """A Resolve timeline takes one frame rate, so clips at 24 and 25 fps go on a timeline each."""
    from types import SimpleNamespace

    mounts = [
        MountedSequence(
            id=str(i), source=tmp_path / f"clip-{i}.mcraw", path=tmp_path / str(i), stamp=(1, 2, 3, 4)
        )
        for i in range(4)
    ]
    rates = [24.0, 25.0, 24.0, 25.0]
    monkeypatch.setattr(
        native,
        "mounted_media",
        lambda mount: SimpleNamespace(
            rate=rates[int(mount.id)],
            info=SimpleNamespace(width=sizes[int(mount.id)][0], height=sizes[int(mount.id)][1]),
        ),
    )
    built: list[tuple[list[str], str]] = []

    def write(path: Path, grouped: list[MountedSequence], **kwargs: Any) -> int:
        built.append(([mount.id for mount in grouped], kwargs["name"]))
        path.write_bytes(b"timeline")
        return 0

    monkeypatch.setattr(native, "write_timeline", write)
    monkeypatch.setattr(native, "open_archive", lambda path: None)
    result = native.send_mounts(mounts, "auto", mode="timeline")
    assert result.launched
    assert [ids for ids, _ in built] == groups
    assert all(name.endswith(label) for (_, name), label in zip(built, labels, strict=True))
    assert [path.stem for path in result.archives] == [name for _, name in built]
    assert says in result.results[0].message


@pytest.mark.parametrize(("separate", "linked"), [(False, True), (True, True), (True, False)])
def test_timeline_send_forwards_audio_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, separate: bool, linked: bool
) -> None:
    calls: list[dict[str, Any]] = []

    def write(path: Path, mounts: list[MountedSequence], **kwargs: Any) -> int:
        calls.append(kwargs)
        path.write_bytes(b"synthetic")
        return 0

    monkeypatch.setattr(native, "write_timeline", write)
    monkeypatch.setattr(native, "open_archive", lambda _: None)
    monkeypatch.setattr(
        native, "timeline_indices", lambda mounts, rotations: [(list(range(len(mounts))), None)]
    )
    result = native.send_mounts([], "auto", mode="timeline", separate_audio=separate, link_audio=linked)
    assert result.launched
    assert calls[0]["separate_audio"] is separate
    assert calls[0]["link_audio"] is linked


def test_shared_original_keeps_each_proxy_and_archive_by_selection_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from mcrawsome.recipe.raw import RawCalibration

    original = MountedSequence(
        id="shared-full", source=tmp_path / "source.mcraw", path=tmp_path / "full", stamp=(1, 2, 3, 4)
    )
    proxies: list[MountedSequence | None] = [
        original.model_copy(
            update={"id": size, "path": tmp_path / size, "raw_settings": RawCalibration(mount_proxy=size)}
        )
        for size in ("half", "quarter")
    ]
    monkeypatch.setattr(
        native,
        "mounted_media",
        lambda _: SimpleNamespace(rate=24.0, info=SimpleNamespace(width=4096, height=2304)),
    )
    calls: list[dict[str, Any]] = []

    def write(path: Path, mounts: list[MountedSequence], **kwargs: Any) -> int:
        assert mounts == [original]
        calls.append(kwargs)
        path.write_bytes(b"archive")
        return 0

    monkeypatch.setattr(native, "write_timeline", write)
    monkeypatch.setattr(native, "open_archive", lambda _: None)
    result = native.send_mounts(
        [original, original], "auto", mode="timeline", rotations=[0, 90], proxies=proxies
    )
    assert result.launched
    assert [c["proxies"] for c in calls] == [[proxies[0]], [proxies[1]]]
    assert [c["rotations"] for c in calls] == [[0], [90]]
    assert result.results[0].archive == result.archives[0]
    assert result.results[1].archive == result.archives[1]
    assert result.results[0].archive != result.results[1].archive
