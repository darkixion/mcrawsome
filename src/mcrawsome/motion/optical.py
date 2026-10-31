"""Image-assisted refinement of a synchronised gyro path.

Persistent, forward/backward checked tracks measure what the gyro missed. Slow drift is
removed along each track before fitting, limiting depth-dependent parallax and moving
subjects. Spatially held-out tracks verify each fit. A damped orientation solve retains
the gyro's slow path and bridges short gaps, without an additional image resampling pass.
This is an independent implementation; no Gyroflow code is incorporated.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from itertools import pairwise
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from mcrawsome.camera.lens import LensCalibration
from mcrawsome.motion.flow import FLOW_WIDTH, Grey, _raw_motion_image
from mcrawsome.motion.geometry import intrinsics, quat_conjugate, quat_from_rotvec, quat_mul, quat_to_matrix
from mcrawsome.motion.orientation import Orientation
from mcrawsome.pipeline.pool import bounded_imap, worker_pool

Array = npt.NDArray[np.float64]
VERSION = 1


class OpticalTrackingError(Exception):
    """The image cannot supply a bounded, complete tracking analysis."""


@dataclass(frozen=True)
class FeatureTracks:
    first: npt.NDArray[np.int64]
    identity: npt.NDArray[np.int64]
    before: npt.NDArray[np.float32]
    after: npt.NDArray[np.float32]
    width: int
    height: int


def track_images(images: Iterable[Grey], check: Callable[[], None] = lambda: None) -> FeatureTracks:
    """Replenish twelve image cells separately, with at most 720 active corners."""
    previous = None
    points = np.empty((0, 1, 2), np.float32)
    identities = np.empty(0, np.int64)
    serial = 0
    starts, ids, before, after = [], [], [], []
    width = height = 0
    observations = 0
    for frame, image in enumerate(images):
        check()
        height, width = image.shape
        if previous is not None and len(points):
            found, ok, _ = cv2.calcOpticalFlowPyrLK(
                previous, image, points, np.empty_like(points), winSize=(21, 21), maxLevel=3
            )
            back, reverse, _ = cv2.calcOpticalFlowPyrLK(
                image, previous, found, np.empty_like(points), winSize=(21, 21), maxLevel=3
            )
            p, q = points[:, 0], found[:, 0]
            valid = (
                (ok[:, 0] != 0)
                & (reverse[:, 0] != 0)
                & (np.linalg.norm(back[:, 0] - p, axis=1) < 0.6)
                & np.isfinite(q).all(axis=1)
                & (q[:, 0] >= 4)
                & (q[:, 0] < width - 4)
                & (q[:, 1] >= 4)
                & (q[:, 1] < height - 4)
            )
            points, identities = found[valid], identities[valid]
            starts.append(np.full(valid.sum(), frame - 1, np.int64))
            ids.append(identities.copy())
            before.append(p[valid])
            after.append(q[valid])
            observations += int(valid.sum())
            if observations > 3_000_000:
                raise OpticalTrackingError("the clip exceeds the optical tracking memory budget")
        mask = np.full(image.shape, 255, np.uint8)
        for x, y in points[:, 0]:
            cv2.circle(mask, (round(float(x)), round(float(y))), 7, 0, -1)
        for row in range(3):
            for col in range(4):
                x0, x1 = col * width // 4, (col + 1) * width // 4
                y0, y1 = row * height // 3, (row + 1) * height // 3
                p = points[:, 0]
                count = np.sum((p[:, 0] >= x0) & (p[:, 0] < x1) & (p[:, 1] >= y0) & (p[:, 1] < y1))
                wanted = min(40 - int(count), 720 - len(points))
                if wanted <= 0:
                    continue
                added = cv2.goodFeaturesToTrack(
                    image[y0:y1, x0:x1],
                    wanted,
                    0.015,
                    7,
                    mask=mask[y0:y1, x0:x1],
                    blockSize=5,
                )
                if added is not None:
                    added += np.array([x0, y0], np.float32)
                    points = np.concatenate([points, added])
                    identities = np.concatenate([identities, np.arange(serial, serial + len(added))])
                    serial += len(added)
        previous = image
    return FeatureTracks(
        np.concatenate(starts) if starts else np.empty(0, np.int64),
        np.concatenate(ids) if ids else np.empty(0, np.int64),
        np.concatenate(before) if before else np.empty((0, 2), np.float32),
        np.concatenate(after) if after else np.empty((0, 2), np.float32),
        width,
        height,
    )


def track_raw(source: Path, frames: int, workers: int, check: Callable[[], None]) -> FeatureTracks:
    """Decode bounded RAW thumbnails, closing pending work before the worker pool."""
    from mcrawsome.motion.optical_cache import measured

    return measured(source, frames, lambda: _track_raw(source, frames, workers, check), check)


def _track_raw(source: Path, frames: int, workers: int, check: Callable[[], None]) -> FeatureTracks:
    with (
        worker_pool(workers) as pool,
        contextlib.closing(
            bounded_imap(
                pool, partial(_raw_motion_image, source), list(range(frames)), window=max(2, workers * 2)
            )
        ) as images,
    ):
        return track_images(images, check)


def track_video(
    ffmpeg: Path,
    source: Path,
    frames: int,
    width: int,
    height: int,
    check: Callable[[], None],
) -> FeatureTracks:
    small_h = round(FLOW_WIDTH * height / width / 2) * 2
    size = FLOW_WIDTH * small_h
    with subprocess.Popen(
        [
            str(ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-noautorotate",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-vf",
            f"scale={FLOW_WIDTH}:{small_h}:flags=area,format=gray",
            "-fps_mode",
            "passthrough",
            "-frames:v",
            str(frames),
            "-f",
            "rawvideo",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        assert process.stdout is not None
        output = process.stdout

        def images() -> Iterable[Grey]:
            for _ in range(frames):
                check()
                data = output.read(size)
                if len(data) != size:
                    raise OpticalTrackingError("the video ended during image analysis")
                yield np.frombuffer(data, np.uint8).reshape(small_h, FLOW_WIDTH)

        try:
            return track_images(images(), check)
        finally:
            process.kill()
            process.wait()


def _rays(points: Array, tracks: FeatureTracks, focal: float, lens: LensCalibration | None) -> Array:
    k = intrinsics(focal, tracks.width, tracks.height)
    if lens is not None:
        try:
            k = np.asarray(lens.matrix_at(tracks.width, tracks.height))
        except ValueError:
            lens = None  # Unknown crop mappings must never be guessed.
    if lens is not None:
        undistort = cv2.fisheye.undistortPoints if lens.model == "opencv_fisheye" else cv2.undistortPoints
        xy = undistort(points.reshape(-1, 1, 2), k, np.asarray(lens.coefficients)).reshape(-1, 2)
    else:
        xy = (points - k[:2, 2]) / np.diag(k)[:2]
    return np.column_stack([xy, np.ones(len(xy))])


def _high_tracks(values: Array, identities: npt.NDArray[np.int64], fps: float) -> tuple[Array, Array]:
    """Reject short tracks; subtract local drift with linear endpoint extrapolation."""
    from scipy.ndimage import gaussian_filter1d  # scipy loads only when needed

    result, usable = np.zeros_like(values), np.zeros(len(values))
    order = np.argsort(identities, kind="stable")
    boundaries = np.r_[0, np.flatnonzero(np.diff(identities[order])) + 1, len(order)]
    sigma = max(1.0, fps * 0.3)
    pad = int(4 * sigma) + 1
    for lo, hi in pairwise(boundaries):
        indices = order[lo:hi]
        if len(indices) < max(8, round(fps * 0.4)):
            continue
        v = values[indices]
        if not np.isfinite(v).all():
            continue
        n = min(5, len(v) // 2)
        left, right = np.mean(np.diff(v[:n], axis=0), axis=0), np.mean(np.diff(v[-n:], axis=0), axis=0)
        padded = np.concatenate(
            [
                v[0] + np.arange(-pad, 0)[:, None] * left,
                v,
                v[-1] + np.arange(1, pad + 1)[:, None] * right,
            ]
        )
        result[indices] = v - gaussian_filter1d(padded, sigma, axis=0)[pad:-pad]
        usable[indices] = 1.0
        usable[indices[:2]] = usable[indices[-2:]] = 0.0
    return result, usable


def _rotation_design(points: Array, scale: bool = False) -> Array:
    x, y = points[:, 0], points[:, 1]
    jacobian = np.stack(
        [
            np.column_stack([-x * y, 1 + x * x, -y]),
            np.column_stack([-1 - y * y, x * y, x]),
        ],
        axis=1,
    )
    if scale:
        # A focus change may change magnification. Treat radial flow as a nuisance
        # measurement, so it cannot pull the rotation estimate. No zoom is applied.
        jacobian = np.concatenate([jacobian, points[:, :2, None]], axis=2)
    return jacobian


def _fit_rotation(
    points: Array,
    residual: Array,
    cells: npt.NDArray[np.int64],
    scale: bool = False,
) -> tuple[Array, float]:
    jacobian = _rotation_design(points, scale)
    spatial = 1 / np.sqrt(np.maximum(np.bincount(cells, minlength=12)[cells], 1))
    weight = spatial.copy()
    rotation = np.zeros(jacobian.shape[-1])
    for _ in range(6):
        a = (jacobian * weight[:, None, None]).reshape(-1, jacobian.shape[-1])
        b = (residual * weight[:, None]).ravel()
        rotation = np.linalg.lstsq(a, b, rcond=None)[0]
        error = np.linalg.norm(np.einsum("nij,j->ni", jacobian, rotation) - residual, axis=1)
        residual_scale = max(float(np.median(error)) * 1.5, 0.00025)
        weight = spatial / np.sqrt(1 + (error / residual_scale) ** 2)
    prediction = np.einsum("nij,j->ni", jacobian, rotation)
    return rotation, float(np.median(np.linalg.norm(residual - prediction, axis=1)))


def _predict_rays(
    base: Orientation,
    ta: Array,
    tb: Array,
    rays: Array,
    ea: Array | None,
    eb: Array | None,
    check: Callable[[], None],
) -> Array:
    """Predict exposure-averaged feature positions, with bounded projection workspaces.

    The average camera matrices approximate the centroid of a blurred feature. The
    approximation is exact for in-plane rotation and second-order for perspective.
    Five-point Gaussian quadrature integrates the shutter interval, including gyro
    acceleration. Without exposure data this is the original centre-time model.
    """
    predicted = np.empty_like(rays)
    nodes, weights = np.polynomial.legendre.leggauss(5)
    for start in range(0, len(rays), 65536):
        check()
        stop = min(start + 65536, len(rays))
        if ea is None or eb is None:
            rotation = quat_to_matrix(
                quat_mul(base.at(tb[start:stop]), quat_conjugate(base.at(ta[start:stop])))
            )
            p = np.einsum("nij,nj->ni", rotation, rays[start:stop])
        else:
            a = np.zeros((stop - start, 3, 3))
            b = np.zeros_like(a)
            for node, weight in zip(nodes / 2, weights / 2, strict=True):
                a += weight * quat_to_matrix(base.at(ta[start:stop] + node * ea[start:stop]))
                b += weight * quat_to_matrix(base.at(tb[start:stop] + node * eb[start:stop]))
            invalid = (np.linalg.det(a) < 0.25) | (np.linalg.det(b) < 0.25)
            a[invalid] = np.eye(3)
            world = np.linalg.solve(a, rays[start:stop, :, None])[..., 0]
            p = np.einsum("nij,nj->ni", b, world)
            p[invalid] = np.nan  # An exposure spanning a large turn has no reliable feature centroid.
        predicted[start:stop] = p
    return predicted


class RefinedOrientation(Orientation):
    def __init__(self, base: Orientation, centres: Array, corrections: Array) -> None:
        self.base, self.centres, self.corrections = base, centres, corrections
        super().__init__(base.times_s, base.quats, base.holes)

    def at(self, t: npt.ArrayLike) -> Array:
        times = np.asarray(t, dtype=np.float64)
        correction = np.stack([np.interp(times, self.centres, self.corrections[:, k]) for k in range(3)], -1)
        return quat_mul(quat_from_rotvec(correction), self.base.at(times))


def _scale_candidates(focus: Array) -> npt.NDArray[np.bool_]:
    """A recorded focus transition permits testing scale wherever metadata is known.

    Magnification-like residuals also arise from translation and parallax between
    focus updates. Each candidate still has to beat rotation alone on held-out
    features. Missing observations cannot establish a transition or support a fit.
    """
    valid = np.isfinite(focus) & (focus >= 0)
    pairs = valid[:-1] & valid[1:]
    changes = pairs & (np.abs(np.diff(focus)) > 0.001)
    return pairs & bool(changes.any())


def refine_orientation(
    base: Orientation,
    centres: Array,
    readout: float,
    tracks: FeatureTracks,
    focal_px: float,
    width: int,
    fps: float,
    lens: LensCalibration | None = None,
    check: Callable[[], None] = lambda: None,
    *,
    pitch: bool = True,
    exposures_s: Array | None = None,
    focus_distance: Array | None = None,
) -> tuple[Orientation, dict[str, float | int | bool]]:
    """Fit small residual rotations; unreliable regions smoothly retain their gyro path."""
    from scipy.linalg import solve_banded  # scipy loads only when needed
    from scipy.ndimage import gaussian_filter1d

    report: dict[str, float | int | bool] = {"applied": False, "measured_pairs": 0}
    if len(centres) < 3 or len(tracks.first) < 40 or np.any(np.diff(centres) <= 0):
        return base, report
    check()
    first = tracks.first
    focal = focal_px * tracks.width / width
    a, b = (
        _rays(tracks.before.astype(np.float64), tracks, focal, lens),
        _rays(tracks.after.astype(np.float64), tracks, focal, lens),
    )
    ta = centres[first] + (tracks.before[:, 1] / tracks.height - 0.5) * readout
    tb = centres[first + 1] + (tracks.after[:, 1] / tracks.height - 0.5) * readout
    ea = eb = None
    if exposures_s is not None:
        if (
            exposures_s.shape != centres.shape
            or not np.isfinite(exposures_s).all()
            or np.any(exposures_s < 0)
        ):
            raise ValueError("Invalid per-frame exposure times for image-assisted refinement")
        if np.any(exposures_s > 0):
            ea, eb = exposures_s[first], exposures_s[first + 1]
            report["exposure_averaged"] = True
    scale_pairs = np.zeros(len(centres) - 1, dtype=bool)
    if focus_distance is not None:
        if focus_distance.shape != centres.shape:
            raise ValueError("Focus metadata must match the recorded frames")
        focus = focus_distance[np.isfinite(focus_distance) & (focus_distance >= 0)]
        if len(focus) > 1 and np.ptp(focus) > 0.01:
            scale_pairs = _scale_candidates(focus_distance)
    report["focus_scale_separated"] = bool(scale_pairs.any())
    predicted = _predict_rays(base, ta, tb, a, ea, eb, check)
    visible = predicted[:, 2] > 0.1
    predicted /= np.maximum(predicted[:, 2:3], 1e-9)
    residual, usable = _high_tracks(b[:, :2] - predicted[:, :2], tracks.identity, fps)
    usable *= base.covers(ta) & base.covers(tb)
    if ea is not None and eb is not None:
        usable *= base.covers(ta - ea / 2) & base.covers(ta + ea / 2)
        usable *= base.covers(tb - eb / 2) & base.covers(tb + eb / 2)
    usable *= visible & np.isfinite(residual).all(axis=1)
    delta, quality = np.zeros((len(centres) - 1, 3)), np.zeros(len(centres) - 1)
    cells = np.clip((tracks.before[:, 0] / tracks.width * 4).astype(int), 0, 3) + 4 * np.clip(
        (tracks.before[:, 1] / tracks.height * 3).astype(int), 0, 2
    )
    bounds = np.searchsorted(first, np.arange(len(centres)))
    verified_scale = 0
    for frame in range(len(delta)):
        if frame % 64 == 0:
            check()
        indices = np.arange(bounds[frame], bounds[frame + 1])
        indices = indices[usable[indices] > 0]
        train, test = indices[tracks.identity[indices] % 3 != 0], indices[tracks.identity[indices] % 3 == 0]
        if (
            len(train) < 30
            or len(test) < 12
            or len(np.unique(cells[train])) < 6
            or len(np.unique(cells[test])) < 4
        ):
            continue
        scale = bool(scale_pairs[frame])
        r, _ = _fit_rotation(predicted[train], residual[train], cells[train], scale)
        if scale:
            simple, _ = _fit_rotation(predicted[train], residual[train], cells[train])
            simple_design = _rotation_design(predicted[test])
            complex_design = _rotation_design(predicted[test], True)
            simple_error = np.median(
                np.linalg.norm(residual[test] - np.einsum("nij,j->ni", simple_design, simple), axis=1)
            )
            complex_error = np.median(
                np.linalg.norm(residual[test] - np.einsum("nij,j->ni", complex_design, r), axis=1)
            )
            # Compare both complete models against the same held-out observations.
            # The extra parameter must earn its freedom, rather than merely fitting
            # training noise or moving foreground. No frame-specific tuning is used.
            if complex_error >= simple_error * 0.9:
                r, scale = simple, False
        design = _rotation_design(predicted[test], scale)
        fit = np.einsum("nij,j->ni", design[:, :, :3], r[:3])
        compared = residual[test] - design[:, :, 3] * r[3] if scale else residual[test]
        before = float(np.median(np.linalg.norm(compared, axis=1)))
        after = float(np.median(np.linalg.norm(compared - fit, axis=1)))
        # A quiet residual is a reliable zero increment, not a tracking gap. Treating
        # zero crossings as gaps would fade the offset just when it reaches its peak.
        if before * focal < 0.15:
            quality[frame] = 1.0
            verified_scale += int(scale)
            continue
        # Independent tracks must improve, and the rotation must be locally plausible.
        if after >= before * 0.95 or np.linalg.norm(r[:3]) > np.radians(3):
            continue
        delta[frame] = r[:3]
        quality[frame] = min(1.0, max(0.25, 1 - after / before) * 2)
        verified_scale += int(scale)
    report["measured_pairs"] = int(np.count_nonzero(quality))
    report["focus_scale_pairs"] = verified_scale
    if np.count_nonzero(quality) < min(12, len(delta) // 2):
        return base, report
    # Solve in the gyro's reference frame: successive residual rotations share a basis.
    camera = quat_to_matrix(base.at(centres))
    world = np.einsum("nji,nj->ni", camera[1:], delta)
    diagonal = np.full(len(centres), 0.06**2)
    diagonal[:-1] += quality
    diagonal[1:] += quality
    band = np.zeros((3, len(centres)))
    band[1], band[0, 1:], band[2, :-1] = diagonal, -quality, -quality
    rhs = np.zeros((len(centres), 3))
    rhs[:-1] -= quality[:, None] * world
    rhs[1:] += quality[:, None] * world
    correction = solve_banded((1, 1), band, rhs)
    # Fade unsupported regions to zero; never borrow a visual correction across a long gap.
    available = (quality > 0).astype(float)
    support = np.r_[available[0], np.maximum(available[:-1], available[1:]), available[-1]]
    # With no measured edge the regularised solve sets an isolated node to zero.
    # Multiplying that zero by a smooth support mask still creates a hard cut.
    # Extend/interpolate the measured correction first, in the shared gyro basis;
    # the mask below then fades long gaps to gyro without snapping at their edges.
    supported = support > 0
    correction = np.stack(
        [np.interp(centres, centres[supported], correction[supported, k]) for k in range(3)], axis=1
    )
    support = gaussian_filter1d(support, max(1.0, fps * 0.12))
    correction *= np.minimum(1.0, support * 2)[:, None]
    correction = np.einsum("nij,nj->ni", camera, correction)
    if not pitch:
        correction[:, 0] = 0
    angle = np.linalg.norm(correction, axis=1)
    correction *= np.minimum(1.0, np.radians(3) / np.maximum(angle, 1e-12))[:, None]
    if np.max(angle) * focal < 0.04:
        return base, report
    report |= {
        "applied": True,
        "max_correction_deg": float(np.degrees(np.linalg.norm(correction, axis=1).max())),
    }
    check()
    return RefinedOrientation(base, centres, correction), report
