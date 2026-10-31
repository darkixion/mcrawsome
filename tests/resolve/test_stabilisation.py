"""Native matrix direction, pixel coordinates and serialisation against rendered research evidence."""

import json
import struct
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from mcrawsome.motion.plan import StabilisationPlan
from mcrawsome.resolve.bin_codec import Reader, decode_wire, unwrap
from mcrawsome.resolve.stabilisation import from_plan, section, version


def plan(rotations: np.ndarray | None = None, **updates: Any) -> StabilisationPlan:
    if rotations is None:
        rotations = Rotation.from_rotvec([[0.008, -0.012, 0.003], [-0.005, 0.004, -0.002]]).as_matrix()
    result = StabilisationPlan(
        (rotations[:, None] if rotations.ndim == 3 else rotations).astype(np.float32),
        np.tile([1.0, 0, 0, 0], (len(rotations), 1)),
        1000.0,
        1.15,
        1600,
        900,
        0,
        0.1,
        0.2,
        1.0,
    )
    return replace(result, **updates)


def blob(value: int | bytes) -> bytes:
    assert isinstance(value, bytes)
    return value


def body(node: ET.Element) -> bytes:
    text = node.findtext(".//Body")
    assert text is not None
    return unwrap(bytes.fromhex(text))[0]


def params(node: ET.Element) -> bytes:
    raw = body(node)
    for number in (3, 9, 1, 6, 2):
        raw = section(raw, number)
    return raw


def entries(raw: bytes, field: int) -> dict[int, bytes]:
    result = {}
    for f in decode_wire(raw):
        if f.number == field:
            key = next(k.value for k in decode_wire(blob(f.value)) if k.number == 1)
            assert isinstance(key, int)
            result[key] = section(blob(f.value), 2)
    return result


def project(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    values = points @ matrix.T
    return np.asarray(values[:, :2] / values[:, 2:3])


@pytest.mark.parametrize("orientation", [1, 3])
@pytest.mark.parametrize("size", [(1600, 900), (1920, 1440)])
def test_native_projection_matches_sensor_rays_and_crop_once(orientation: int, size: tuple[int, int]) -> None:
    width, height = size
    p = plan(width=width, height=height, rotate_180=orientation == 3)
    result = from_plan(p, [0, 1], orientation=orientation)
    # Independently project rays through the raw camera, then return to upright pixels.
    points = np.array(
        [[0.0, 0, 1], [width / 2, height / 2, 1], [width - 1.0, height - 1, 1], [351.0, 249, 1]]
    )
    scale = np.diag([1280 / width, 720 / height, 1])
    for index in range(2):
        raw = points.copy()
        if orientation == 3:
            raw[:, :2] = [width - 1, height - 1] - raw[:, :2]
        rays = np.column_stack(
            [(raw[:, 0] - width / 2) / p.focal_px, (raw[:, 1] - height / 2) / p.focal_px, np.ones(len(raw))]
        )
        rotated = rays @ p.rotations[index, 0].T
        expected = rotated[:, :2] / rotated[:, 2:3] * p.focal_px + [width / 2, height / 2]
        if orientation == 3:
            expected = [width - 1, height - 1] - expected
        expected = (expected - [width / 2, height / 2]) * p.zoom + [width / 2, height / 2]
        # Native decode: inverse stored matrix, rescale, then centre zoom from scalar.
        forward = np.linalg.inv(scale) @ np.linalg.inv(result.matrices[index].astype(float)) @ scale
        actual = (project(forward, points) - [width / 2, height / 2]) * result.zoom + [width / 2, height / 2]
        np.testing.assert_allclose(actual, expected, atol=0.0003)
    neutral = from_plan(
        plan(np.tile(np.eye(3), (2, 1, 1)), width=width, height=height, rotate_180=orientation == 3),
        [0, 1],
        orientation=orientation,
    )
    np.testing.assert_allclose(neutral.matrices, np.tile(np.eye(3), (2, 1, 1)), atol=1e-7)


def test_three_rendered_research_matrices_are_reproduced() -> None:
    reference = json.loads((Path(__file__).parent / "data/perspective_21_1.json").read_text())
    p = plan(
        np.array(reference["rotations"]),
        width=reference["width"],
        height=reference["height"],
        focal_px=reference["focal_px"],
        rotate_180=True,
    )
    result = from_plan(p, [0, 1, 2], orientation=3)
    np.testing.assert_array_equal(result.matrices, np.array(reference["matrices"], np.float32))


@pytest.mark.parametrize("recorded_flip", [False, True])
def test_saved_half_turn_projects_in_the_final_display_orientation(recorded_flip: bool) -> None:
    source = plan(rotate_180=recorded_flip)
    final_orientation = 1 if recorded_flip else 3
    actual = from_plan(source, [0, 1], orientation=final_orientation, rotation=180)
    expected = from_plan(replace(source, rotate_180=not recorded_flip), [0, 1], orientation=final_orientation)
    np.testing.assert_array_equal(actual.matrices, expected.matrices)
    with pytest.raises(ValueError, match="orientation"):
        from_plan(source, [0, 1], orientation=final_orientation, rotation=90)


def test_trim_offsets_repeated_frames_and_local_zigzag_indices() -> None:
    p = plan()
    result = from_plan(p, [1, 1, 0], orientation=1)
    np.testing.assert_array_equal(result.matrices[0], result.matrices[1])
    frames = decode_wire(section(params(version(result, (1920, 1080))), 4))
    assert len(frames) == 3
    for index, f in enumerate(frames):
        values = decode_wire(blob(f.value))
        assert next((k.value for k in values if k.number == 1), 0) == 2 * index
        frame = entries(blob(f.value), 2)
        assert set(frame) == {0x10300015, 0x10300017, 0x1030001A, 0x1030001B}
        block = section(frame[0x10300015], 5)
        assert len(block) == 80
        assert struct.unpack(">5Q", block[:40]) == (2, 1280, 720, 0, 0)
        assert struct.unpack("<f", block[40:44])[0] == pytest.approx(1 / p.zoom)
        np.testing.assert_array_equal(np.frombuffer(block[44:], "<f4").reshape(3, 3), result.matrices[index])
        for key in (0x10300017, 0x1030001A):
            np.testing.assert_array_equal(
                [struct.unpack("<f", blob(k.value))[0] for k in decode_wire(section(frame[key], 10))],
                np.eye(3).ravel(),
            )
        assert next(k.value for k in decode_wire(frame[0x1030001B]) if k.number == 2) == 0
    globals_ = entries(params(version(result, (1920, 1080))), 3)
    assert globals_[0x10300016] == b"\x10\x08"
    assert globals_[0x1030001D] == b"\x20\x00"
    assert b"22 serialization::archive" in section(globals_[0x10300018], 5)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"rotations": np.tile(np.eye(3), (2, 2, 1, 1))}, "frame-global"),
        ({"rotate_180": True}, "orientation"),
        ({"focal_px": float("nan")}, "focal"),
        ({"zoom": 0.9}, "crop"),
        ({"coverage": 0.5}, "coverage"),
        ({"rotations": np.zeros((2, 1, 3, 3), np.float32)}, "rotation"),
    ],
)
def test_invalid_plans_are_refused(change: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        from_plan(plan(**change), [0, 1], orientation=1)


@pytest.mark.parametrize("mapping", [[-1], [2], [0.5], []])
def test_invalid_source_indexing_is_refused(mapping: list[Any]) -> None:
    with pytest.raises(ValueError, match="mapping"):
        from_plan(plan(), mapping, orientation=1)


def test_geometry_and_singular_cache_validation() -> None:
    result = from_plan(plan(), [0, 1], orientation=1)
    with pytest.raises(ValueError, match="geometry"):
        result.validate(2, 1601, 900, 1)
    with pytest.raises(ValueError, match="frame count"):
        result.validate(3, 1600, 900, 1)
    with pytest.raises(ValueError, match="Singular"):
        replace(result, matrices=np.zeros((2, 3, 3), np.float32)).validate(2, 1600, 900, 1)
    with pytest.raises(ValueError, match="aspect"):
        version(result, (1920, 1440))


def test_cache_longer_than_generic_wire_field_limit_is_complete() -> None:
    result = from_plan(plan(), [0] * 4100, orientation=1)
    reader = Reader(section(params(version(result, (1600, 900))), 4))
    count = 0
    while reader.position < len(reader.data):
        assert reader.varint() == 10
        frame = decode_wire(reader.take(reader.varint()))
        assert next((f.value for f in frame if f.number == 1), 0) == count * 2
        count += 1
    assert count == 4100


def test_matrix_generation_and_cache_serialization_honour_cancellation() -> None:
    def cancel() -> None:
        raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        from_plan(plan(), [0], orientation=1, check=cancel)
    result = from_plan(plan(), [0], orientation=1)
    with pytest.raises(InterruptedError):
        version(result, (1600, 900), check=cancel)


def test_oversized_cache_is_explained_without_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.resolve import stabilisation

    monkeypatch.setattr(stabilisation, "_MAX_CACHE_BYTES", 1000)
    result = from_plan(plan(), [0] * 10, orientation=1)
    with pytest.raises(ValueError, match="exceeds 8 MiB"):
        version(result, (1600, 900))


@pytest.mark.parametrize("orientation", [1, 3])
@pytest.mark.parametrize("size", [(3840, 2160), (1920, 1440)])
@pytest.mark.parametrize("turn", [90, 180, 270])
def test_clip_attribute_turn_rebases_serialized_perspective_cache(
    orientation: int, size: tuple[int, int], turn: int
) -> None:
    width, height = size
    p = plan(width=width, height=height, rotate_180=orientation == 3)
    correction = from_plan(p, [0, 1], orientation=orientation)
    turns = {
        90: np.array([[0, -1, height], [1, 0, 0], [0, 0, 1.0]]),
        180: np.array([[-1, 0, width], [0, -1, height], [0, 0, 1.0]]),
        270: np.array([[0, 1, 0], [-1, 0, width], [0, 0, 1.0]]),
    }
    display = turns[turn]
    output = (height, width) if turn % 180 else size
    before = np.diag([1280 / width, 720 / height, 1])
    after = np.diag([1280 / output[0], 720 / output[1], 1])
    node = version(correction, output, display_rotation=turn)
    points = np.array([[0, 0, 1], [output[0] / 2, output[1] / 2, 1], [*output, 1]])
    for index, frame in enumerate(decode_wire(section(params(node), 4))):
        block = section(entries(blob(frame.value), 2)[0x10300015], 5)
        matrix = np.frombuffer(block[44:], "<f4").reshape(3, 3).astype(float)
        expected = (
            display
            @ np.linalg.inv(before)
            @ np.linalg.inv(correction.matrices[index])
            @ before
            @ np.linalg.inv(display)
        )
        actual = np.linalg.inv(after) @ np.linalg.inv(matrix) @ after
        np.testing.assert_allclose(project(actual, points), project(expected, points), atol=0.001)
        assert struct.unpack("<f", block[40:44])[0] == pytest.approx(1 / p.zoom)


def test_native_portrait_control_difference_identifies_cache_coordinate_order() -> None:
    from mcrawsome.motion.geometry import intrinsics, zoom_matrix

    fixture = json.loads((Path(__file__).parent / "data/clip_rotation_perspective_21_1.json").read_text())
    width, height = fixture["width"], fixture["height"]
    k = intrinsics(fixture["focal_px"], width, height)
    original = np.diag([1280 / width, 720 / height, 1])
    oriented = np.diag([1280 / height, 720 / width, 1])
    display = np.array([[0, -1, height], [1, 0, 0], [0, 0, 1.0]])
    scale = np.diag([0.5, 0.5, 1])
    x, y = np.meshgrid(np.linspace(108, 972, 9), np.linspace(192, 1728, 9))
    points = np.column_stack([x.ravel(), y.ravel(), np.ones(x.size)])
    for rotation, measured in zip(
        fixture["raw_rotations"], fixture["unconverted_perspective_from_camera"], strict=True
    ):
        forward = k @ np.array(rotation) @ np.linalg.inv(k)
        camera = scale @ display @ zoom_matrix(fixture["zoom"], width, height) @ forward
        unconverted = (
            scale
            @ zoom_matrix(fixture["zoom"], height, width)
            @ np.linalg.inv(oriented)
            @ original
            @ forward
            @ np.linalg.inv(original)
            @ oriented
            @ display
        )
        predicted = unconverted @ np.linalg.inv(camera)
        delta = project(predicted, points) - project(np.array(measured), points)
        assert np.sqrt(np.mean(np.sum(delta**2, axis=1))) < 0.25


def test_projection_batches_repeated_frames_without_rebuilding_intrinsics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = plan()
    mapping = [1, 0, 1] * 200
    expected = from_plan(source, [0, 1], orientation=1).matrices
    original = np.linalg.inv
    sizes: list[int] = []

    def inverse(matrix: np.ndarray) -> np.ndarray:
        sizes.append(len(matrix) if matrix.ndim == 3 else 1)
        return original(matrix)

    monkeypatch.setattr(np.linalg, "inv", inverse)
    actual = from_plan(source, mapping, orientation=1)
    np.testing.assert_array_equal(actual.matrices, expected[mapping])
    assert len(sizes) < 10
    assert max(sizes) <= 256
