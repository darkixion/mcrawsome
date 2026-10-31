"""Pinned application code survives replacement; garbage collection skips live services."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mcrawsome.mount import runtime
from mcrawsome.paths import state_dir


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = tmp_path / "installed/mcrawsome"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "value.txt").write_text("old application")
    vendor = tmp_path / "installed/vendor"
    vendor.mkdir()
    (vendor / "dependency.py").write_text("old dependency")
    (package / "MOUNT_RUNTIME").write_text(json.dumps({"id": "a" * 64, "protocol": 1, "vendor": str(vendor)}))
    monkeypatch.setattr(runtime, "_package", lambda: package)
    return package


def test_pin_copies_application_and_vendor_before_replacement(installed: Path) -> None:
    with runtime.runtime_command() as command:
        root = Path(command[-1]).parent
        assert command[:2] == [sys.executable, "-I"]
        assert (root / "mcrawsome/value.txt").read_text() == "old application"
        assert (root / "vendor/dependency.py").read_text() == "old dependency"
        assert (root / "mcrawsome/VENDOR").read_text().strip() == str(root / "vendor")
    shutil.rmtree(installed.parent)
    assert (root / "mcrawsome/value.txt").read_text() == "old application"
    assert (root / "vendor/dependency.py").read_text() == "old dependency"
    runtime.collect_runtimes()
    assert not root.exists()


def test_gc_retains_launching_process_before_ready_is_written(installed: Path) -> None:
    with runtime.runtime_command() as command:
        launcher = Path(command[-1])
        launcher.write_text("import time\ntime.sleep(30)\n")
        process = subprocess.Popen(command)
    try:
        runtime.collect_runtimes()
        assert launcher.is_file()
    finally:
        process.terminate()
        process.wait(timeout=5)
    runtime.collect_runtimes()
    assert not launcher.parent.exists()


def test_runtime_copy_failure_never_publishes_partial_runtime(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.errors import McError

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("Test disk full")

    monkeypatch.setattr(shutil, "copytree", fail)
    with pytest.raises(McError, match="Test disk full"), runtime.runtime_command():
        pytest.fail("Must not launch")
    assert not (state_dir() / "mount-runtimes" / ("a" * 64)).exists()


def test_runtime_gc_uses_neither_symlink_targets_nor_unknown_directories(
    installed: Path, tmp_path: Path
) -> None:
    root = state_dir() / "mount-runtimes"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").touch()
    (root / ("b" * 64)).symlink_to(outside, target_is_directory=True)
    (root / "unrelated").mkdir()
    runtime.collect_runtimes()
    assert (outside / "keep").is_file()
    assert (root / "unrelated").is_dir()


def test_gc_preserves_pre_exec_lease_without_holding_global_lock(installed: Path) -> None:
    with runtime.runtime_command() as command:
        root = Path(command[-1]).parent
        # GC can run while preparation is waiting for exec; the parent lease protects the copy.
        runtime.collect_runtimes()
        assert root.is_dir()
    runtime.collect_runtimes()
    assert not root.exists()
