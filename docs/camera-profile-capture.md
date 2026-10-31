# Guided camera profile capture

Open **Settings → Camera profiles → Manage profiles → Show calibration target**. Choose **Use
full screen**, line up the display in the phone's camera frame, and start the countdown. The phone
records in its own camera app; MCRAWsome does not remotely control it.

Use the physical lens, sensor crop, resolution and frame rate you want to profile. For the supplied
Pixel 8 example, that means the 25 mm equivalent lens in the 2048 × 1536, 24 fps MotionCam mode.
Different crops or sensor readout modes need separate profiles. Uniform resizing can preserve
optical geometry, but it does not establish that a different recording mode uses the same sensor area.

Use the largest available **flat** display. Keep the display still, with a white border around the
square checkerboard. Lock focus, exposure and white balance; disable electronic stabilisation,
digital zoom and automatic lens switching. Keep the target sharp at both distances. A large display
lets you work farther away and closer to the focus distance of ordinary footage.

Start around **1/125–1/250 second** to limit motion blur, with the lowest practical ISO and without
clipping the white squares. Inspect a short recording for screen flicker, dark bands and moiré. If
there are bands, try an exposure spanning a whole display refresh period, such as **1/60 second
for a 60 Hz display**, and move more slowly. Display PWM and camera timing differ, so check the
recording instead of assuming any shutter speed is universally flicker-free.

The guide counts down each movement: align, rotate up and back to centre, down and back, left and
back, right and back, then gentle roll and oblique views. Keep the lens in the same position during
the rotation sweeps. Aim towards the edge without losing the entire checkerboard for most of the
sweep; cropped boards cannot contribute to this detector. The oblique views deliberately change the
lens position to add perspective variation. For a view from the left, move the phone sideways left
at the same height, then turn it right to centre the whole checkerboard. Return to your starting
position before moving right, above or below, and aim back at the board's centre each time. The
arrows describe the phone's movement; these steps keep the board centred rather than asking for
corner placement at the same time. A pause lets you move farther away before repeating.
The full sequence takes about five minutes. Pause, repeat a step and optional spoken prompts are
available. Hiding the guide, resizing the display or exiting fullscreen pauses the countdown.

Transfer the original recording to the computer, then select or open it in the profile form.
Analysis supports `.mcraw`, `.mp4`, `.mov` and `.mkv`, without applying display rotation or lens warps.
The calibration file picker accepts readable video with any transfer function, including linear,
SDR and unidentified profiles. It probes the file directly without adding it to the normal Library
or Open clips workflow, so calibration does not require a Log recording.
It samples at most 480 frames across the whole recording, detects the 9 × 6 internal corners of
the 10 × 7 target, rejects blurred/repeated views, and retains at most 90 distinct poses.

Choose **Standard lens** or **Fisheye lens**. Standard fits five OpenCV radial/tangential coefficients;
JSON interchange pads these to twelve. Fisheye fits four coefficients. No geometry from the
supplied reference is copied into a newly measured profile.

Every fifth selected view is held out before fitting. The remaining views fit the lens; poor training
views may be rejected once, without using held-out errors to choose them. Quality checks cover
training and independent validation error, image coverage, perspective variation, distance variation,
plausible intrinsics, and radial monotonicity over the observed area. RAW focus metadata is also
checked for changes. At least 20 distinct views are needed, with at least 15 for fitting. The current
acceptance limit is 1.5 pixels in the original recording coordinates for both RMS errors. These are
quality heuristics; a real recording is needed to establish practical accuracy.

**Save JSON** writes a lens-profile document containing all the top-level fields in the supplied
reference. Extra evidence lives under `mcrawsome`: independent validation, per-view errors and frame
positions, coverage grid, tilt/distance diagnostics, uncertainty estimates where available, recorded
capture metadata, sampling details, assumptions and unmeasured fields. Display guide events, when
present, are explicitly not synchronised to the recording. No physical square size is needed to
estimate intrinsics; physical translations and focal length in millimetres are not inferred.

To also measure gyro axes, sync and rolling-shutter readout, expand the optional gyro section and
open a **separate 30–60 second recording** of a distant, static, detailed scene. Use the same camera
and recording mode, slow pans/tilts/rolls in both directions and gyro logging. Keep the matching
`.gcsv` beside a RAW recording; Direct Log uses embedded telemetry. The existing motion calibrator
supplies trusted values, with confidence and notes included in JSON. An untrusted fit leaves those
fields unknown and preserves the lens result. A motion fit never replaces measured lens geometry.

Failed lens quality checks still allow saving a diagnostic JSON, but the interface prevents adding
that result as an active profile. **Add profile to MCRAWsome** installs an accepted profile in the
existing camera-profile directory; existing names are never overwritten. Installation is separate
from JSON export. Lens geometry is retained for interchange; MCRAWsome does not currently apply
lens correction to developed pixels. A real camera capture and comparison are needed to establish
practical accuracy.

Implementation reference: [OpenCV calibration](https://docs.opencv.org/4.x/dc/dbb/tutorial_py_calibration.html).

User profiles can be removed under **Manage profiles → Installed profiles → Delete → Delete profile**.
Deleted names can be reused for a new capture. Built-in profiles are read-only; exported JSON and
recordings are retained.
