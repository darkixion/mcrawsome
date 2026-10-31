"""A global remosaic switch is captured by each preview, export and virtual media generation."""

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from quad_clips import quad_clip, recorded_values

from mcrawsome.errors import McError
from mcrawsome.mount import manager as mounts
from mcrawsome.mount.manager import MountedSequence, MountManager, save_record
from mcrawsome.mount.service import ServingSequence
from mcrawsome.output.chunks import SETTINGS_CHANGED
from mcrawsome.output.dng_export import export_dng
from mcrawsome.output.export import frame_job, picture_key, read_clip_facts
from mcrawsome.preview.session import PreviewSession, PreviewSettings
from mcrawsome.recipe.load import build_recipe
from mcrawsome.server.settings import InterfaceSettings, save_settings


def test_preview_and_render_jobs_keep_the_selected_mode(tmp_path: Path) -> None:
    path = quad_clip(tmp_path / "quad.mcraw")
    recipe = build_recipe([{"develop": {"exposure": 0}}])
    save_settings(InterfaceSettings(quad_bayer_remosaic=False))
    job = frame_job(path, recipe, read_clip_facts(path, recipe))
    assert job.quad_bayer_remosaic is False
    assert picture_key(job) != picture_key(replace(job, quad_bayer_remosaic=True))
    with PreviewSession(path, recipe, PreviewSettings(width=64, gpu=False)) as session:
        save_settings(InterfaceSettings(quad_bayer_remosaic=True))
        session.set_recipe(recipe)
        reader, _ = session._raw()
        np.testing.assert_array_equal(reader.raw_frame(0).data, recorded_values(path, 0))
        assert session._job.quad_bayer_remosaic is False


def test_dng_resume_restarts_after_the_toggle_changes(tmp_path: Path) -> None:
    path = quad_clip(tmp_path / "quad.mcraw")
    output = tmp_path / "dng"
    recipe = build_recipe(
        [{"develop": {"exposure": 0}, "output": {"kind": "dng", "dng_compression": "none"}}]
    )
    save_settings(InterfaceSettings(quad_bayer_remosaic=False))
    with pytest.raises(McError) as stopped:
        export_dng(path, output, recipe, workers=1, stop=lambda: True)
    assert stopped.value.code == "paused"
    save_settings(InterfaceSettings(quad_bayer_remosaic=True))
    result = export_dng(path, output, recipe, workers=1)
    assert SETTINGS_CHANGED in result.warnings
    data = np.frombuffer((output / "quad-000000.dng").read_bytes()[-128 * 64 * 2 :], "<u2")
    assert not np.array_equal(data.reshape(64, 128), recorded_values(path, 0))


def test_mount_mode_is_immutable_and_part_of_its_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = quad_clip(tmp_path / "quad.mcraw")
    manager = MountManager()
    monkeypatch.setattr(mounts, "availability", lambda: (True, "Available"))
    monkeypatch.setattr(manager, "_start", lambda record: record)
    save_settings(InterfaceSettings(quad_bayer_remosaic=False))
    disabled = manager.mount(path)
    save_settings(InterfaceSettings(quad_bayer_remosaic=True))
    enabled = manager.mount(path)
    assert disabled.id != enabled.id
    assert disabled.quad_bayer_remosaic is False
    folder = tmp_path / "service"
    folder.mkdir()
    save_record(folder, disabled)
    restored = MountedSequence.model_validate_json((folder / "sequence.json").read_text())
    assert restored.quad_bayer_remosaic is False
    with ServingSequence(restored, folder) as sequence:
        name = sequence.names()[0]
        served = sequence.read(name, 0, sequence.size(name))
        assert served[-128 * 64 * 2 :] == recorded_values(path, 0).astype("<u2").tobytes()


def test_deflicker_keeps_the_export_choice_when_preferences_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.develop import deflicker
    from mcrawsome.io.clip import Clip

    path = quad_clip(tmp_path / "quad.mcraw")
    save_settings(InterfaceSettings(quad_bayer_remosaic=True))
    monkeypatch.setattr(deflicker, "worker_pool", lambda _: nullcontext())
    monkeypatch.setattr(
        deflicker, "bounded_imap", lambda pool, fn, items, window: (fn(item) for item in items)
    )
    try:
        deflicker.measure_gains(path, 1.0, 1.0, 1, quad_bayer_remosaic=False)
        assert deflicker._clips[path].quad_bayer_remosaic is False
        deflicker.measure_gains(path, 1.0, 1.0, 1, quad_bayer_remosaic=True)
        assert deflicker._clips[path].quad_bayer_remosaic is True
        with Clip(path, quad_bayer_remosaic=False) as recorded:
            np.testing.assert_array_equal(recorded.raw_frame(0).data, recorded_values(path, 0))
    finally:
        deflicker._close_clips()
