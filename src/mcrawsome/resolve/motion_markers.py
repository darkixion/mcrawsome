"""Review recording metadata, bounded RAW samples and calibrated sensors in the mounted timebase."""

from __future__ import annotations

import logging
from collections.abc import Callable

import numpy as np

from mcrawsome.analysis.motion_markers import suggest
from mcrawsome.analysis.review_markers import REVIEW_CATEGORIES, REVIEW_COLOURS, blur, focus, separate, tilt
from mcrawsome.io.gcsv import GcsvError, read_gcsv
from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.geometry import quat_to_matrix
from mcrawsome.motion.horizon import reference_up
from mcrawsome.motion.orientation import AxisMapping, Orientation
from mcrawsome.motion.plan import StabilisationPlan
from mcrawsome.motion.raw_gyro import GyroError, RawGyro, raw_gyro
from mcrawsome.motion.sync import raw_frame_times
from mcrawsome.mount.manager import MountedSequence
from mcrawsome.mount.sequence import Sequence, source_stamp
from mcrawsome.pipeline.stabilise import GyroFit, camera_profile
from mcrawsome.recipe.raw import RawCalibration
from mcrawsome.resolve.bin_archive import Media
from mcrawsome.resolve.gyro import clip_recipe
from mcrawsome.resolve.markers import Marker

logger = logging.getLogger(__name__)


def prepare(
    mount: MountedSequence,
    media: Media,
    fitted: GyroFit | None = None,
    check: Callable[[], None] = lambda: None,
    *,
    plan: StabilisationPlan | None = None,
    display_rotation: int = 0,
) -> tuple[list[Marker], Callable[[], None]]:
    check()
    categories = clip_recipe(mount.source).motion.review_categories
    path = mount.source.with_suffix(".gcsv")
    stamp = source_stamp(path) if path.is_file() else None
    log: GyroLog | None = None
    if stamp is not None:
        try:
            log = fitted.log if fitted is not None else read_gcsv(path)
        except GcsvError as exc:
            logger.info("Review markers omit unreadable sensors for %s: %s", mount.source.name, exc)
    with Sequence(
        mount.source,
        raw_settings=RawCalibration(mount_frame_rate=mount.raw_settings.mount_frame_rate),
        frame_range=mount.frame_range,
        quad_bayer_remosaic=False,
    ) as seq:
        if seq.stamp != mount.stamp or len(seq.sources) != media.frames or float(seq.rate) != media.rate:
            raise ValueError("Mounted source timing changed during review marker analysis")
        frames = []
        for index in range(seq.clip.frame_count):
            check()
            frames.append(seq.clip.frame_timing(index))
        recorded: RawGyro | None = None
        if seq.clip.has_recorded_motion:  # in place of a .gcsv, as when stabilising
            try:
                recorded = raw_gyro(seq.clip)
            except GyroError as exc:
                logger.info("Review markers omit unreadable sensors for %s: %s", mount.source.name, exc)
            if recorded is not None:
                log = fitted.log if fitted is not None else recorded.log
        sources = np.asarray(seq.sources)
        result = focus(
            np.array(
                [
                    frames[i].focus_distance if frames[i].focus_distance is not None else np.nan
                    for i in sources
                ]
            ),
            media.rate,
        )
        stamps = seq.clip.timestamps_ns.astype(np.float64)
        step = float(np.median(np.diff(stamps))) if len(stamps) > 1 else 0
        for gap_index in np.flatnonzero(np.diff(stamps) > step * 1.5) + 1 if step > 0 else []:
            output = np.flatnonzero(sources == gap_index)
            if len(output):
                result.append(
                    Marker(
                        int(output[0]),
                        1,
                        REVIEW_COLOURS["Recording gap"],
                        "Recording gap",
                        f"Timestamp gap: {(stamps[gap_index] - stamps[gap_index - 1]) / 1e6:.1f} ms "
                        "gap before this frame. Check the source for missing motion.",
                    )
                )
        # About one sample per second, no more than 120 decodes. Repeated output frames share a decode.
        count = min(120, max(1, int(np.ceil(media.frames / media.rate))))
        positions = (
            np.unique(np.linspace(0, media.frames - 1, count, dtype=int))
            if (categories.clipping or categories.low_signal)
            else np.array([], dtype=int)
        )
        sampled: dict[int, tuple[float, float, float]] = {}
        for position in positions:
            check()
            index = int(sources[position])
            if index not in sampled:
                raw = seq.clip.raw_frame(index)
                stride = max(1, int(np.ceil(np.sqrt(raw.data.size / 262144)))) * 2
                black = np.array(raw.metadata.black_level).reshape(2, 2)
                white = raw.metadata.white_level
                if not np.isfinite(black).all() or not np.isfinite(white) or white <= float(black.max()) + 2:
                    continue
                planes = [
                    raw.data[y::stride, x::stride].astype(np.float64) for y in range(2) for x in range(2)
                ]
                normal = np.concatenate(
                    [
                        ((plane - black.flat[k]) / (white - black.flat[k])).ravel()
                        for k, plane in enumerate(planes)
                    ]
                )
                clipped = np.concatenate([(plane >= white - 2).ravel() for plane in planes])
                sampled[index] = (
                    float(clipped.mean()),
                    float(np.median(normal)),
                    float(np.quantile(normal, 0.95)),
                )
            clipping, median, high = sampled[index]
            note = (
                f"RAW sample at frame {position}; {len(positions)} samples across the clip (limit 120). "
                "Events between samples may be missed. Review the picture and intended exposure."
            )
            if clipping >= 0.02:
                result.append(
                    Marker(
                        int(position),
                        1,
                        REVIEW_COLOURS["Highlight clipping"],
                        "Highlight clipping",
                        f"{clipping:.1%} of sampled sensor values reach recorded white. {note}",
                    )
                )
            elif median < 0.003 and high < 0.015:
                result.append(
                    Marker(
                        int(position),
                        1,
                        REVIEW_COLOURS["Low recorded signal"],
                        "Low recorded signal",
                        "Most sampled sensor values are near black. This can be an intentional dark scene. "
                        + note,
                    )
                )
        if log is not None:
            try:
                if recorded is not None:
                    times = recorded.frame_times_s
                else:
                    received = np.array([f.received_ms for f in frames], dtype=np.float64)
                    times = raw_frame_times(seq.clip.timestamps_ns, received, log)
                profile = (
                    fitted.profile
                    if fitted is not None
                    else camera_profile(seq.clip, clip_recipe(mount.source))
                )
                if fitted is not None:
                    offset = fitted.fit.offset_s
                else:
                    offset = (
                        recorded.sync_s(profile)
                        if recorded is not None
                        else (profile.gcsv_sync_ms or 0) / 1000
                    )
                centres = (
                    times
                    + np.array([f.exposure_time_ns for f in frames]) / 2e9
                    + (profile.readout_ms or 0) / 2000
                    + offset
                )
                result.extend(
                    marker
                    for marker in suggest(log, centres[sources], media.rate, check=check)
                    if marker.name == "Possible shake"
                )
                if categories.blur:
                    from mcrawsome.analysis.motion_summary import frame_blur, rates

                    chosen = centres[sources]
                    exposure = np.array([f.exposure_time_ns for f in frames])[sources] / 1e9
                    focal = fitted.fit.focal_px if fitted is not None else profile.focal_px
                    try:
                        swept = rates(log, float(chosen.min()), float(chosen.max()))
                        found = frame_blur(swept, chosen, exposure, focal or 0.78 * frames[0].width)
                        result.extend(blur(found, media.rate))
                    except ValueError as exc:  # blur alone is skipped; tilt still follows
                        logger.info("Review markers omit motion blur for %s: %s", mount.source.name, exc)
                axes = profile.gcsv_gravity_axes
                if axes is not None:
                    physical = Orientation.from_gyro(
                        log,
                        AxisMapping(
                            (axes.axes[1], axes.axes[0], axes.axes[2]),
                            (-axes.signs[1], axes.signs[0], -axes.signs[2]),
                        ),
                    )
                    chosen = centres[sources]
                    up = np.einsum(
                        "nij,nj->ni",
                        quat_to_matrix(physical.at(chosen)),
                        reference_up(log, physical, axes, chosen, check),
                    )
                    recorded_turn = 180 if frames[0].orientation == 3 else 0
                    result.extend(tilt(up, media.rate, "Recorded tilt", recorded_turn))
                    if plan is not None and categories.remaining_tilt:
                        remaining = np.einsum(
                            "nij,nj->ni", plan.rotations[sources, plan.rotations.shape[1] // 2], up
                        )
                        result.extend(
                            tilt(
                                remaining,
                                media.rate,
                                "Remaining tilt",
                                (recorded_turn + display_rotation) % 360,
                            )
                        )
            except ValueError as exc:
                logger.info(
                    "Review markers omit unavailable sensor measurements for %s: %s", mount.source.name, exc
                )
        seq.clip.check_unchanged()
        result = separate(
            [marker for marker in result if getattr(categories, REVIEW_CATEGORIES[marker.name])], media.frames
        )

    def unchanged() -> None:
        check()
        if source_stamp(mount.source) != mount.stamp:
            raise ValueError("Recording changed during review marker analysis")
        current = source_stamp(path) if path.is_file() else None
        if current != stamp:
            raise ValueError("Gyro log changed during review marker analysis")

    unchanged()
    return result, unchanged
