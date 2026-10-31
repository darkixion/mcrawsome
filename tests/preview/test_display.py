"""Local display leases bound memory and protect a frame until its upload is acknowledged."""

import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest


def test_display_slots_preserve_pixels_and_reject_stale_acknowledgements(tmp_path: Path) -> None:
    from mcrawsome.preview.display import DisplayFrames, DisplayReader

    first_image = np.arange(96, dtype=np.uint8).reshape(4, 8, 3)
    second_image = 255 - first_image
    with DisplayFrames(32, folder=tmp_path, slots=1) as frames:
        first = frames.write(first_image, lambda: None)
        assert first.channels == 4  # BGRX: GL takes it without converting each pixel
        path = tmp_path / first.name
        assert path.stat().st_mode & 0o777 == 0o600
        with DisplayReader(first, folder=tmp_path) as reader:
            actual = reader.pixels(first)
            np.testing.assert_array_equal(actual[..., :3], first_image)
            del actual
        assert frames.release(first)
        second = frames.write(second_image, lambda: None)
        assert not frames.release(first)
        with DisplayReader(second, folder=tmp_path) as reader:
            actual = reader.pixels(second)
            np.testing.assert_array_equal(actual[..., :3], second_image)
            del actual
        assert frames.release(second)
    assert not path.exists()


def test_an_older_engines_three_byte_lease_still_reads(tmp_path: Path) -> None:
    import os

    from mcrawsome.preview.display import DisplayLease, DisplayReader

    image = np.arange(96, dtype=np.uint8).reshape(4, 8, 3)
    path = tmp_path / "mcrawsome-preview-1-older"
    path.write_bytes(image.tobytes())
    os.chmod(path, 0o600)
    lease = DisplayLease.model_validate(
        {"name": path.name, "slot": 0, "token": "0" * 32, "width": 8, "height": 4, "stride": 96, "slots": 1}
    )
    assert lease.channels == 3
    with DisplayReader(lease, folder=tmp_path) as reader:
        actual = reader.pixels(lease)
        np.testing.assert_array_equal(actual, image)
        del actual
    with pytest.raises(ValueError, match="bounds"):  # four bytes a pixel do not fit three-byte slots
        DisplayLease.model_validate(lease.model_dump() | {"channels": 4})


def test_waiting_for_display_credit_is_cancellable(tmp_path: Path) -> None:
    from mcrawsome.preview.display import DisplayFrames

    cancelled = threading.Event()
    waiting = threading.Event()

    def check() -> None:
        waiting.set()
        if cancelled.is_set():
            raise CancelledError()

    image = np.zeros((4, 8, 3), np.uint8)
    with DisplayFrames(32, folder=tmp_path, slots=1) as frames, ThreadPoolExecutor(1) as executor:
        first = frames.write(image, lambda: None)
        task = executor.submit(frames.write, image, check)
        assert waiting.wait(2)
        cancelled.set()
        with pytest.raises(CancelledError):
            task.result(timeout=2)
        assert frames.release(first)


def test_reader_rejects_paths_symlinks_and_invalid_shapes(tmp_path: Path) -> None:
    from mcrawsome.preview.display import DisplayFrames, DisplayReader

    with DisplayFrames(32, folder=tmp_path) as frames:
        lease = frames.write(np.zeros((4, 8, 3), np.uint8), lambda: None)
        for patch in ({"name": "../outside"}, {"slot": 99}, {"width": 9999}, {"stride": 1}):
            with pytest.raises(ValueError, match=r"validation error|display"):
                DisplayReader(lease.model_copy(update=patch), folder=tmp_path)
        path = tmp_path / lease.name
        replacement = tmp_path / "elsewhere"
        path.rename(replacement)
        path.symlink_to(replacement)
        with pytest.raises(OSError, match="symbolic link"):
            DisplayReader(lease, folder=tmp_path)


def test_native_source_bypasses_jpeg_and_releases_cancelled_queued_frames(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.preview.display import DisplayFrames, DisplayReader
    from mcrawsome.preview.session import PreviewSession, PreviewSettings
    from mcrawsome.recipe.load import build_recipe

    def encode(*args: object) -> bytes:
        pytest.fail("native source encoded JPEG")

    monkeypatch.setattr(PreviewSession, "_encode", encode)
    with (
        DisplayFrames(128 * 64, folder=tmp_path) as frames,
        PreviewSession(
            tiny_raw, build_recipe([{"develop": {"exposure": 0}}]), PreviewSettings(gpu=False)
        ) as session,
    ):
        stream = session.pipeline.stream(0, display=frames)
        found = stream.receive(timeout=2)
        assert found is not None
        draft = found[1]
        assert draft.frame.jpeg == b""
        assert draft.frame.display is not None
        with DisplayReader(draft.frame.display, folder=tmp_path) as reader:
            pixels = reader.pixels(draft.frame.display)
            assert pixels.shape == (64, 128, 4)
            del pixels
        draft.frame.discard()
        session.pipeline.stop_stream()
        assert frames.in_use == 0


def test_stream_cancels_while_the_client_holds_all_display_credit(tiny_raw: Path, tmp_path: Path) -> None:
    from mcrawsome.preview.display import DisplayFrames
    from mcrawsome.preview.session import PreviewSession, PreviewSettings
    from mcrawsome.recipe.load import build_recipe

    # One held slot is sufficient to force the encoder to wait for client credit.
    with (
        DisplayFrames(128 * 64, folder=tmp_path, slots=1) as frames,
        PreviewSession(tiny_raw, build_recipe([{}]), PreviewSettings(gpu=False)) as session,
    ):
        stream = session.pipeline.stream(0, display=frames)
        found = stream.receive(timeout=2)
        assert found is not None
        first = found[1].frame
        assert frames.in_use == 1
        # Joining the cancelled producer must not depend on an acknowledgement from the client.
        with ThreadPoolExecutor(1) as executor:
            executor.submit(session.pipeline.stop_stream).result(timeout=2)
        first.discard()
        assert frames.in_use == 0
