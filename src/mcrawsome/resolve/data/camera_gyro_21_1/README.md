# Native frame-global Camera Gyro version

Measured from Resolve 21.1.1.0010 (schema 17). This private native format is based
on the owner's rendered and re-exported Camera Gyro / Perspective comparison of
20 October 2026. It is not a public interchange schema.

`version.xml` contains a complete local version table with synthetic identities,
zero XML save time, placeholder geometry, one identity frame, zero queue time,
and neutral focal/zoom metadata. It contains no source paths, images or gyro
samples. Runtime code supplies all geometry, focal length, crop, poses and times;
the timeline writer refreshes version identities. Unknown binary state is kept,
including Body/3 fields 4, 5, 8 and 12. The initial comparison files cleared these
as presumed save counters and crashed on opening; they have been restored to the
observed values. A portable regression compares the entire Body envelope,
not only stabilisation parameters, against the successful native research file.

Body uses byte 0x81 + Zstandard and protobuf wire encoding. Stabilisation lives
at 3/9/1/6/2, with globals in repeated field 3 and frame records in field 4.
Mode 3 is Camera Gyro. Global 0x10300016 stays 8 and 0x1030001d stays 1; the latter
is the observed native `ID_ADVANCED_STABILIZATION_PARAMS_DIRTY` value, not a
general gyro-availability flag. Preserve the Boost settings' other fields.

Each frame has local zigzag index 2*i and three parameters: identity matrix
0x10300017; 0x1030001e Variant field 5 containing 13 xyzw float32 quaternions
(identity target, twelve equal camera poses); 0x1030001f Variant field 12/1
containing float32 relative local nanoseconds and identity xyzw target. No
Perspective 0x10300015 warp block is present. Repeated source frames retain the
same correction while their local queue times advance at the output rate.

Shared projection is outside that parameter set: 3/10, repeated field 1 entry
with ID 0xc00000cd, Variant field 5. Preserve the six-byte prefix 01000c000c00,
then encode little-endian uint16 recorded width/height, float32 focal pixels
and float32 centre zoom. Crop appears here once, never in the quaternions.

Research's orientation-3, 3840x2160, 24 fps, 1920x1080 test rendered all 96
corrections: median planned-mapping error 0.02450 pixels; Camera Gyro/Perspective
median geometric difference 0.05256 pixels. Both Camera Gyro clips retained
their complete parameter sets and projection bytes on native re-export. The
portable fixture preserves three input rotations and their rendered native
quaternions, with no media identities or paths. Corrected TIFF-1 native renders
also agree with Perspective across 48 matching frames at 3840x2160 landscape
and 1080x1920 portrait: median grid RMS 0.0760/0.0656 pixels respectively,
maximum 0.1257 pixels. Native rendering handles TIFF orientation itself, so both
TIFF 1 and 3 receive inverse(raw rotation), without an extra D conjugation.
The TIFF-1 portable fixture includes measured mappings before and after that fix.
These relative comparisons used the earlier Edit display turn. The current writer
uses the separate native CinemaDNG Clip Attributes rotation (Radiometry field 19),
matching the owner's saved portrait reference; no display turn is folded into
these poses or projection. All 48 native portrait frames with Clip Attributes rotation match the verified
landscape output after rotation/resize: median grid RMS 0.0120 px, maximum
0.1342 px. RAW-to-render registration confirms the intended 1.1 crop without
extra canvas sizing. Perspective comparison matrices require their own display
coordinate rebasing; the camera poses remain unchanged.
