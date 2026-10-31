"""The everyday gate selects tests without changing the full gate."""

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("quick", [False, True])
def test_gate_pytest_selection(tmp_path: Path, quick: bool) -> None:
    commands = tmp_path / "commands"
    binary = tmp_path / "bin"
    binary.mkdir()
    uv = binary / "uv"
    uv.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['GATE_COMMANDS'], 'a') as out:\n"
        "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    uv.chmod(0o755)
    npm = binary / "npm"
    npm.write_text("#!/bin/sh\nexit 0\n")
    npm.chmod(0o755)
    command = [str(Path(__file__).parents[1] / "gates.sh")]
    if quick:
        command.append("--quick")
    command += ["--basetemp", str(tmp_path / "pytest")]
    subprocess.run(
        command,
        env=os.environ
        | {
            "PATH": f"{binary}:{os.environ['PATH']}",
            "GATE_COMMANDS": str(commands),
            "MCRAWSOME_GATE_WORKERS": "3",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    calls = [json.loads(line) for line in commands.read_text().splitlines()]
    expected = ["run", "pytest", "-q", "-W", "error", "-n", "3", "--dist", "load"]
    if quick:
        expected += ["-m", "not slow and not local"]
    assert calls[-1] == [*expected, "--basetemp", str(tmp_path / "pytest")]


def test_gate_help_explains_both_modes(tmp_path: Path) -> None:
    stub = tmp_path / "uv"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    npm = tmp_path / "npm"
    npm.write_text("#!/bin/sh\nexit 0\n")
    npm.chmod(0o755)
    result = subprocess.run(
        [str(Path(__file__).parents[1] / "gates.sh"), "--help"],
        env=os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--quick" in result.stdout
    assert "not slow and not local" in result.stdout
    assert "full suite" in result.stdout
