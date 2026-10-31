from pathlib import Path

import numpy as np
import pytest

from mcrawsome.io.gcsv import GcsvError, read_gcsv

SAMPLE = """GYROFLOW IMU LOG
version,1.2
id,Google.Pixel.10.Pro.XL
orientation,XYZ
timestamp,1791449410602
vendor,MotionCam
tscale,0.0025
gscale,0.00000001
ascale,0.00000001
t,gx,gy,gz,ax,ay,az
0,100000000,0,0,981000000,0,0,
1,0,200000000,0,981000000,0,0,
"""


def test_reads_scaled_values(tmp_path: Path) -> None:
    p = tmp_path / "a.gcsv"
    p.write_text(SAMPLE)
    log = read_gcsv(p)
    np.testing.assert_allclose(log.times_s, [0.0, 0.0025])
    np.testing.assert_allclose(log.gyro[0], [1.0, 0.0, 0.0])
    np.testing.assert_allclose(log.gyro[1], [0.0, 2.0, 0.0])
    assert log.accel is not None
    assert log.start_epoch_s == pytest.approx(1791449410.602)
    assert log.orientation == "XYZ"


def test_rejects_files_that_are_not_gyroflow_logs(tmp_path: Path) -> None:
    p = tmp_path / "a.gcsv"
    p.write_text("hello\nworld\n")
    with pytest.raises(GcsvError, match="Gyroflow"):
        read_gcsv(p)
