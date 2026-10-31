import json
from pathlib import Path

import numpy as np

from mcrawsome.io.container import ContainerWriter, read_audio_chunk, read_frame_record, read_index
from mcrawsome.io.trim import trim


def _make(path: Path) -> None:
    with ContainerWriter(path, {"k": 1}) as w:
        for i in range(10):
            ts = i * 40_000_000
            w.add_frame(ts, bytes([i]) * 4, json.dumps({"i": i}).encode())
            w.add_audio(np.full(4, i, dtype=np.int16).tobytes(), ts)


def test_trim_keeps_selected_frames_and_overlapping_audio(tmp_path: Path) -> None:
    src, dst = tmp_path / "src.mcraw", tmp_path / "dst.mcraw"
    _make(src)
    trim(src, dst, start=3, count=4)
    with dst.open("rb") as f:
        idx = read_index(f)
        assert idx.metadata == {"k": 1}
        assert [json.loads(read_frame_record(f, fr.offset)[1])["i"] for fr in idx.frames] == [3, 4, 5, 6]
        audio_values = [int(read_audio_chunk(f, a.offset)[1][0]) for a in idx.audio]
        assert audio_values[0] <= 3
        assert audio_values[-1] >= 6
