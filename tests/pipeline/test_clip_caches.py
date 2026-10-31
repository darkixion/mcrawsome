import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = """
import sys
from pathlib import Path
from mcrawsome.motion.flow import _raw_motion_image
from mcrawsome.output.dng_export import DngSettings, _write_frame
from mcrawsome.pipeline.render import FrameJob, render_frame
from fractions import Fraction

clip = Path(sys.argv[1])
_raw_motion_image(clip, 0)
_write_frame(clip, Path(sys.argv[2]), Fraction(30), DngSettings(0.0, 0.0, "none", (0.5, 1.0, 0.5)), (0, 0))
render_frame(FrameJob(clip_path=clip, exposure_stops=0.0, neutral=(0.5, 1.0, 0.5)), 0)
"""


@pytest.mark.fixture
def test_clips_kept_open_for_the_process_are_closed_at_exit(fixture_12bit: Path, tmp_path: Path) -> None:
    folder = tmp_path / "dng"
    folder.mkdir()
    run = subprocess.run(
        [sys.executable, "-W", "always::ResourceWarning", "-c", SCRIPT, str(fixture_12bit), str(folder)],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert "ResourceWarning" not in run.stderr
    assert "Exception ignored" not in run.stderr
