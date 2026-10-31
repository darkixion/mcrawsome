"""Capture intel_gpu_top JSON with monotonic timestamps and trial phase markers.

Run this helper through sudo only when counter access is authorised. It changes no permissions.
The target folder must already exist. It can run beside run.py in a separate terminal.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=90)
    args = parser.parse_args()
    if not args.output.is_dir() or not 0 < args.seconds <= 600:
        parser.error("need an existing output folder and 0 < seconds <= 600")
    phases = []
    with (args.output / "gpu-top.json").open("w") as log:
        started = time.monotonic()
        process = subprocess.Popen(["intel_gpu_top", "-J", "-s", "200"], stdout=log)
        try:
            while time.monotonic() - started < args.seconds:
                try:
                    phase = json.loads((args.output / "phase.json").read_text())["name"]
                except (OSError, json.JSONDecodeError):
                    phase = "startup"
                phases.append({"time": time.monotonic(), "phase": phase})
                if process.poll() is not None:
                    raise RuntimeError("intel_gpu_top exited before capture ended")
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            process.wait(timeout=5)
            (args.output / "gpu-top-timeline.json").write_text(
                json.dumps({"started": started, "finished": time.monotonic(), "phases": phases}, indent=2)
                + "\n"
            )


if __name__ == "__main__":
    main()
