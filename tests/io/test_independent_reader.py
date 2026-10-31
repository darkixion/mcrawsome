import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from mcrawsome.io import clip as module
from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ContainerWriter, read_frame_record
from mcrawsome.io.metadata import FrameMetadata


@pytest.mark.parametrize("decoded", [False, True])
def test_typed_frame_reads_validate_json_without_an_intermediate_dict(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch, decoded: bool
) -> None:
    with Clip(tiny_raw) as reader:
        expected = FrameMetadata.model_validate(reader.raw_frame_metadata(1))
        monkeypatch.setattr(json, "loads", lambda *_a, **_k: pytest.fail("parsed JSON twice"))
        actual = reader.raw_frame(1).metadata if decoded else reader.frame_metadata(1)
        assert actual == expected


@pytest.mark.parametrize(
    ("overrides", "valid"),
    [
        ({"iso": "100.5", "recvdTimestampMs": "1791132083080", "unknown": {"keep": [1, 2]}}, True),
        ({"asShotNeutral": [0.5, 1.0]}, False),
        ({"dynamicWhiteLevel": None}, False),
    ],
)
def test_direct_frame_validation_keeps_coercion_unknown_fields_and_rejections(
    tiny_raw: Path, tmp_path: Path, overrides: dict[str, object], valid: bool
) -> None:
    with Clip(tiny_raw) as source:
        raw = source.raw_frame_metadata(0) | overrides
        payload, _ = read_frame_record(source._f, source._index.frames[0].offset)
        container = source.raw_metadata
    path = tmp_path / "metadata.mcraw"
    with ContainerWriter(path, container) as writer:
        writer.add_frame(0, payload, json.dumps(raw).encode())
    with Clip(path) as reader:
        assert reader.raw_frame_metadata(0) == raw
        if valid:
            expected = FrameMetadata.model_validate(raw)
            assert reader.frame_metadata(0) == reader.raw_frame(0).metadata == expected
        else:
            with pytest.raises(ValidationError):
                reader.frame_metadata(0)
            with pytest.raises(ValidationError):
                reader.raw_frame(0)


def test_readers_reuse_the_index_but_have_independent_file_positions_and_lifetimes(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Clip(tiny_raw)
    expected = [original.raw_frame(i).data.copy() for i in range(3)]
    monkeypatch.setattr(module, "read_index", lambda _: pytest.fail("reread the container index"))
    reader = original.independent_reader()
    try:
        assert reader.metadata is not original.metadata
        with ThreadPoolExecutor(max_workers=2) as pool:

            def metadata() -> None:
                for i in range(60):
                    original.frame_metadata(i % 3)

            def frames() -> None:
                for i in range(60):
                    np.testing.assert_array_equal(reader.raw_frame(i % 3).data, expected[i % 3])

            a, b = pool.submit(metadata), pool.submit(frames)
            a.result(timeout=5)
            b.result(timeout=5)
        original.close()
        np.testing.assert_array_equal(reader.raw_frame(2).data, expected[2])
    finally:
        reader.close()
        original.close()


@pytest.mark.parametrize("replace", [False, True])
def test_new_reader_rejects_changed_or_replaced_sources(tiny_raw: Path, replace: bool) -> None:
    with Clip(tiny_raw) as clip:
        if replace:
            data = tiny_raw.read_bytes()
            tiny_raw.unlink()
            tiny_raw.write_bytes(data)
        else:
            with tiny_raw.open("ab") as stream:
                stream.write(b"changed")
        with pytest.raises(OSError, match="source recording changed"):
            clip.independent_reader()


def test_playback_reuses_first_frame_layout_without_rereading_metadata(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Clip.frame_metadata
    calls: dict[int, int] = {}

    def metadata(reader: Clip, index: int):  # type: ignore[no-untyped-def]
        if index == 0:
            calls[id(reader)] = calls.get(id(reader), 0) + 1
        return original(reader, index)

    monkeypatch.setattr(Clip, "frame_metadata", metadata)
    with Clip(tiny_raw) as reader:
        for index in (1, 2, 1, 2):
            expected = reader.raw_frame(index)
            actual = reader.draft_frame(index)
            np.testing.assert_array_equal(actual.data, expected.data)
        assert calls[id(reader)] <= 1
        with reader.independent_reader() as other:
            reader.close()
            for index in (1, 2):
                other.draft_frame(index)
            assert calls.get(id(other), 0) <= 1
