from pathlib import Path

import cv2
import numpy as np
import pytest

from mcrawsome.output.export import read_clip_facts, source_job
from mcrawsome.preview.session import PreviewSession, PreviewSettings
from mcrawsome.recipe.load import build_recipe


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_raw_preview_turns_and_updates_dimensions_without_stretching(tiny_raw: Path, rotation: int) -> None:
    base = build_recipe([{"develop": {"exposure": 0}, "motion": {"stabilise": "off"}}])
    turned = build_recipe([base.model_dump(), {"develop": {"rotation": rotation}}])
    with PreviewSession(tiny_raw, base, PreviewSettings(gpu=False)) as session:
        session.set_recipe(base, fast=True)
        normal = cv2.imdecode(np.frombuffer(session.fast(0).jpeg, np.uint8), cv2.IMREAD_COLOR)
        session.set_recipe(turned, fast=True)
        actual = cv2.imdecode(np.frombuffer(session.fast(0).jpeg, np.uint8), cv2.IMREAD_COLOR)
        assert normal is not None
        assert actual is not None
        expected = np.rot90(normal, -rotation // 90)
        assert actual.shape == expected.shape
        assert np.abs(actual.astype(float) - expected).mean() < 3  # JPEG encoding after the turn
        assert session.full_size == ((64, 128) if rotation % 180 else (128, 64))
        assert session.size == ((32, 64) if rotation % 180 else (64, 32))
        frame = cv2.imdecode(np.frombuffer(session.frame(0), np.uint8), cv2.IMREAD_COLOR)
        assert frame is not None
        assert frame.shape == expected.shape
        source = session.source_encoder(0)()
        assert source.size == session.full_size
        refined = session.refined(0)
        image = cv2.imdecode(np.frombuffer(refined.jpeg, np.uint8), cv2.IMREAD_COLOR)
        assert image is not None
        assert image.shape == expected.shape
        session.set_recipe(base, fast=True)
        restored = cv2.imdecode(np.frombuffer(session.fast(0).jpeg, np.uint8), cv2.IMREAD_COLOR)
        np.testing.assert_array_equal(restored, normal)
    facts = read_clip_facts(tiny_raw, turned, audio=False)
    job = source_job(tiny_raw, turned, facts)
    assert job.rotation == rotation
