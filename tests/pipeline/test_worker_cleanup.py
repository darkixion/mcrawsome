import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.fixture
def test_cached_raw_clip_is_closed_when_the_process_exits(fixture_12bit: Path) -> None:
    script = f"""from pathlib import Path
from mcrawsome.pipeline.render import FrameJob, _worker
_worker(FrameJob(Path({str(fixture_12bit)!r}), exposure_stops=0, neutral=(1, 1, 1)))
"""
    run = subprocess.run([sys.executable, "-Werror", "-c", script], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert "ResourceWarning" not in run.stderr
    assert "Exception ignored" not in run.stderr
