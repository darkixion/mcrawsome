"""Camera Gyro projection, orientation and complete native cache against rendered evidence."""

import json
import struct
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scipy.spatial.transform import Rotation
from test_stabilisation import blob, body, entries, params, plan, project

from mcrawsome.motion.geometry import intrinsics, zoom_matrix
from mcrawsome.motion.plan import StabilisationPlan
from mcrawsome.resolve import camera_gyro
from mcrawsome.resolve.bin_codec import Reader, decode_wire, patch_wire
from mcrawsome.resolve.camera_gyro import CameraGyroCorrections, from_plan, version
from mcrawsome.resolve.stabilisation import at_path, section
from mcrawsome.resolve.stabilisation import from_plan as perspective_from_plan


def projection(node: ET.Element) -> bytes:
    shared = section(section(body(node), 3), 10)
    return section(entries(shared, 1)[0xC00000CD], 5)


def prepare(
    p: StabilisationPlan | None = None,
    mapping: list[Any] | None = None,
    orientation: int = 1,
    **kwargs: Any,
) -> CameraGyroCorrections:
    return from_plan(
        p if p is not None else plan(),
        [0, 1] if mapping is None else mapping,
        orientation=orientation,
        fps=kwargs.pop("fps", 24),
        centre_offset_s=kwargs.pop("centre_offset_s", 0.012),
        **kwargs,
    )


def test_rendered_camera_gyro_quaternions_and_lens_payload_are_reproduced() -> None:
    fixture = json.loads((Path(__file__).parent / "data/camera_gyro_21_1.json").read_text())
    p = plan(
        np.array(fixture["rotations"]),
        width=fixture["width"],
        height=fixture["height"],
        focal_px=fixture["focal_px"],
        rotate_180=True,
    )
    correction = prepare(p, [0, 1, 2], orientation=3)
    np.testing.assert_array_equal(correction.quaternions, np.array(fixture["camera_quaternions"], np.float32))
    node = version(correction, (1920, 1080))
    assert projection(node).hex() == fixture["projection_hex"]
    raw = body(node)
    envelope = at_path(raw, (3, 9, 1, 6, 2), lambda b: patch_wire(b, {4: b""}))
    assert envelope.hex() == fixture["native_body_without_frames_hex"]


def test_tiff1_native_renders_already_apply_the_orientation_basis() -> None:
    fixture = json.loads((Path(__file__).parent / "data/camera_gyro_tiff1_21_1.json").read_text())
    p = plan(
        np.array(fixture["raw_rotations"]),
        width=fixture["width"],
        height=fixture["height"],
        focal_px=fixture["focal_px"],
        zoom=fixture["zoom"],
    )
    camera = prepare(p, [0, 1, 2])
    k = intrinsics(p.focal_px, p.width, p.height)
    z = zoom_matrix(p.zoom, p.width, p.height)
    xx, yy = np.meshgrid(
        np.linspace(p.width * 0.1, p.width * 0.9, 9), np.linspace(p.height * 0.1, p.height * 0.9, 9)
    )
    grid = np.column_stack([xx.ravel(), yy.ravel(), np.ones(xx.size)])
    for index, (supplied, measured) in enumerate(
        zip(fixture["supplied_camera_quaternions"], fixture["measured_camera_to_perspective"], strict=True)
    ):
        perspective = z @ k @ p.rotations[index, 0].astype(float) @ np.linalg.inv(k)
        # The old v2 frame supplied an extra D conjugation. Its actual rendered
        # discrepancy identifies the native TIFF-1 projector independently.
        native_v2 = z @ k @ Rotation.from_quat(supplied).inv().as_matrix() @ np.linalg.inv(k)
        predicted = perspective @ np.linalg.inv(native_v2)
        delta = project(predicted, grid) - project(np.array(measured), grid)
        assert np.sqrt(np.mean(np.sum(delta**2, axis=1))) < 0.25
        assert np.sqrt(np.mean(np.sum((project(predicted, grid) - grid[:, :2]) ** 2, axis=1))) > 1
        # Correct production poses supply inverse raw directly for TIFF 1 too.
        native_fixed = (
            z @ k @ Rotation.from_quat(camera.quaternions[index]).inv().as_matrix() @ np.linalg.inv(k)
        )
        np.testing.assert_allclose(project(native_fixed, grid), project(perspective, grid), atol=0.0003)
        measured_fixed = np.array(fixture["corrected_camera_to_perspective"][index])
        residual = project(perspective @ np.linalg.inv(native_fixed), grid) - project(measured_fixed, grid)
        assert np.sqrt(np.mean(np.sum(residual**2, axis=1))) < 0.25


@pytest.mark.parametrize("orientation", [1, 3])
@pytest.mark.parametrize("size", [(3840, 2160), (1920, 1440)])
@pytest.mark.parametrize("turn", [0, 90, 180, 270])
def test_native_ray_mapping_matches_perspective_in_display_coordinates(
    orientation: int, size: tuple[int, int], turn: int
) -> None:
    width, height = size
    p = plan(width=width, height=height, focal_px=width * 0.8, rotate_180=orientation == 3)
    camera = prepare(p, orientation=orientation)
    reference = perspective_from_plan(p, [0, 1], orientation=orientation)
    k = intrinsics(p.focal_px, width, height)
    d = np.diag([-1, -1, 1]) if orientation == 3 else np.eye(3)
    scale = np.diag([1280 / width, 720 / height, 1])
    points = np.array([[0, 0, 1], [width / 2, height / 2, 1], [width - 1, height - 1, 1]])
    displays = [
        np.eye(3),
        np.array([[0, -1, height - 1], [1, 0, 0], [0, 0, 1]]),
        np.array([[-1, 0, width - 1], [0, -1, height - 1], [0, 0, 1]]),
        np.array([[0, 1, 0], [-1, 0, width - 1], [0, 0, 1]]),
    ]
    display = displays[turn // 90]
    for index, q in enumerate(camera.quaternions):
        # Native convention from the independently rendered Camera Gyro experiment.
        forward = k @ d @ Rotation.from_quat(q).inv().as_matrix() @ d @ np.linalg.inv(k)
        expected = np.linalg.inv(scale) @ np.linalg.inv(reference.matrices[index].astype(float)) @ scale
        actual_points = project(display @ zoom_matrix(p.zoom, width, height) @ forward, points)
        expected_points = project(display @ zoom_matrix(p.zoom, width, height) @ expected, points)
        # Perspective flips about (W-1,H-1); native pinhole poses use (W/2,H/2).
        # Preserve the researched convention instead of claiming pixel identity.
        np.testing.assert_allclose(actual_points, expected_points, atol=0.08)
    output = (height, width) if turn % 180 else size
    assert len(projection(version(camera, output, display_rotation=turn))) == 18


@pytest.mark.parametrize("recorded_flip", [False, True])
def test_saved_half_turn_uses_final_tiff_orientation(recorded_flip: bool) -> None:
    p = plan(rotate_180=recorded_flip)
    orientation = 1 if recorded_flip else 3
    saved = prepare(p, orientation=orientation, rotation=180)
    expected = prepare(replace(p, rotate_180=not recorded_flip), orientation=orientation)
    np.testing.assert_array_equal(saved.quaternions, expected.quaternions)


def test_shared_projection_has_recorded_geometry_and_crop_only_once() -> None:
    p = plan()
    camera = prepare(p)
    neutral_crop = prepare(replace(p, zoom=1))
    np.testing.assert_array_equal(camera.quaternions, neutral_crop.quaternions)
    node = version(camera, (900, 1600), display_rotation=90)
    block = projection(node)
    assert block[:6] == bytes.fromhex("01000c000c00")
    assert struct.unpack("<HHff", block[6:]) == pytest.approx((1600, 900, 1000, 1.15))
    assert 0xC00000CD not in entries(params(node), 3)


def test_complete_native_frames_local_indices_repeats_and_relative_queue_times() -> None:
    camera = prepare(mapping=[1, 1, 0], fps=30, centre_offset_s=0.007)
    np.testing.assert_array_equal(camera.quaternions[0], camera.quaternions[1])
    node = version(camera, (1920, 1080))
    globals_ = entries(params(node), 3)
    assert globals_[0x10300016] == b"\x10\x08"
    assert globals_[0x1030001D] == b"\x20\x01"  # native DIRTY state, not gyro availability
    assert b" 0 3 0 " in section(globals_[0x10300018], 5)
    for i, frame in enumerate(decode_wire(section(params(node), 4))):
        assert next((f.value for f in decode_wire(blob(frame.value)) if f.number == 1), 0) == 2 * i
        fields = entries(blob(frame.value), 2)
        assert set(fields) == {0x10300017, 0x1030001E, 0x1030001F}
        poses = np.frombuffer(section(fields[0x1030001E], 5), "<f4").reshape(13, 4)
        np.testing.assert_array_equal(poses[0], [0, 0, 0, 1])
        np.testing.assert_array_equal(poses[1:], np.tile(camera.quaternions[i], (12, 1)))
        np.testing.assert_allclose(np.linalg.norm(poses, axis=1), 1, atol=1e-7)
        queue = struct.unpack("<5f", section(section(fields[0x1030001F], 12), 1))
        assert queue[0] == pytest.approx((i / 30 + 0.007) * 1e9, rel=1e-7)
        assert queue[1:] == (0, 0, 0, 1)
        intermediate = decode_wire(section(fields[0x10300017], 10))
        np.testing.assert_array_equal(
            [struct.unpack("<f", blob(f.value))[0] for f in intermediate], np.eye(3).ravel()
        )


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"rotations": np.tile(np.eye(3), (2, 2, 1, 1))}, "frame-global"),
        ({"rotate_180": True}, "orientation"),
        ({"focal_px": float("nan")}, "focal"),
        ({"zoom": 0.9}, "crop"),
        ({"coverage": 0.5}, "coverage"),
        ({"rotations": np.zeros((2, 1, 3, 3), np.float32)}, "rotation"),
        ({"width": 65536}, "geometry"),
    ],
)
def test_invalid_plans_are_refused(change: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        prepare(plan(**change))


@pytest.mark.parametrize("mapping", [[-1], [2], [0.5], []])
def test_invalid_source_mapping_is_refused(mapping: list[Any]) -> None:
    with pytest.raises(ValueError, match="mapping"):
        prepare(mapping=mapping)


@pytest.mark.parametrize("kwargs", [{"fps": 0}, {"fps": float("nan")}, {"centre_offset_s": -1}])
def test_invalid_queue_timing_is_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="offset"):
        prepare(**kwargs)


def test_invalid_pose_geometry_timing_and_aspect_are_refused() -> None:
    camera = prepare()
    with pytest.raises(ValueError, match="geometry"):
        camera.validate(2, 1601, 900, 1)
    with pytest.raises(ValueError, match="poses"):
        replace(camera, quaternions=np.zeros((2, 4), np.float32)).validate(2, 1600, 900, 1)
    with pytest.raises(ValueError, match="timing"):
        replace(camera, queue_times_ns=np.zeros(2, np.float32)).validate(2, 1600, 900, 1)
    with pytest.raises(ValueError, match="aspect"):
        version(camera, (1920, 1440))


def test_long_cache_is_complete_and_cancellation_and_bounds_are_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    camera = prepare(mapping=[0] * 4100)
    reader = Reader(section(params(version(camera, (1600, 900))), 4))
    count = 0
    while reader.position < len(reader.data):
        assert reader.varint() == 10
        frame = decode_wire(reader.take(reader.varint()))
        assert next((f.value for f in frame if f.number == 1), 0) == count * 2
        count += 1
    assert count == 4100

    def cancel() -> None:
        raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        prepare(check=cancel)
    with pytest.raises(InterruptedError):
        version(camera, (1600, 900), check=cancel)
    monkeypatch.setattr(camera_gyro, "_MAX_CACHE_BYTES", 1000)
    with pytest.raises(ValueError, match="exceeds 8 MiB"):
        version(camera, (1600, 900))


def _row_plan() -> StabilisationPlan:
    """Twelve row bands, each turned a little further about the vertical axis (a rolling-shutter pan)."""
    base = plan()
    rows = [Rotation.from_rotvec([0.0, 0.001 * band, 0.0]).as_matrix() for band in range(12)]
    rotations = np.stack([np.stack([r @ base.rotations[f, 0].astype(float) for r in rows]) for f in range(2)])
    return replace(base, rotations=rotations.astype(np.float32))


def test_rolling_shutter_rows_put_each_band_in_its_own_row_slot_in_order() -> None:
    p = _row_plan()
    camera = prepare(p, rows=True)
    assert camera.quaternions.shape == (2, 12, 4)
    for frame in range(2):
        for band in range(12):
            expected = Rotation.from_matrix(p.rotations[frame, band].astype(float)).inv().as_quat()
            np.testing.assert_allclose(camera.quaternions[frame, band], expected, atol=1e-6)
    node = version(camera, (1600, 900))
    for i, record in enumerate(decode_wire(section(params(node), 4))):
        fields = entries(blob(record.value), 2)
        poses = np.frombuffer(section(fields[0x1030001E], 5), "<f4").reshape(13, 4)
        np.testing.assert_array_equal(poses[0], [0, 0, 0, 1])
        np.testing.assert_array_equal(poses[1:], camera.quaternions[i])


def test_rolling_shutter_rows_need_twelve_bands_and_frame_global_refuses_bands() -> None:
    with pytest.raises(ValueError, match="12-band"):
        prepare(plan(), rows=True)
    with pytest.raises(ValueError, match="frame-global"):
        prepare(_row_plan())


def test_row_conversion_batches_repeated_source_frames_with_bounded_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _row_plan()
    mapping = [1, 0, 1] * 200
    sizes: list[int] = []

    class RotationProbe:
        @staticmethod
        def from_matrix(matrices: np.ndarray) -> Rotation:
            sizes.append(len(matrices) if matrices.ndim == 3 else 1)
            return Rotation.from_matrix(matrices)

    monkeypatch.setattr("scipy.spatial.transform.Rotation", RotationProbe)
    actual = from_plan(source, mapping, orientation=1, fps=24, centre_offset_s=0.01, rows=True)
    for frame in (0, 255, 256, 511, 599):
        expected = Rotation.from_matrix(source.rotations[mapping[frame]].astype(float)).inv().as_quat()
        np.testing.assert_allclose(actual.quaternions[frame], expected, atol=1e-7)
    assert len(sizes) <= 4
    assert max(sizes) <= 4096


def test_native_cache_prepares_unchanged_wire_fields_once(monkeypatch: pytest.MonkeyPatch) -> None:
    camera = prepare(mapping=[0] * 600)
    original = decode_wire
    calls = 0

    def decode(data: bytes):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original(data)

    monkeypatch.setattr(camera_gyro, "decode_wire", decode)
    version(camera, (1600, 900))
    assert calls < 40
