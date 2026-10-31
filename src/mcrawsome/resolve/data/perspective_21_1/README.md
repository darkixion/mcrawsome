# Native frame-global Perspective version

Measured from Resolve 21.1.1.0010 (database schema 17), using the owner's native Perspective
analysis export. This is a private, version-specific format, not a public interchange schema.

`version.xml` contains only the local version table. Object IDs are synthetic, save counters are
zero, the frame list contains one neutral frame, and no source paths, names, images or gyro samples
remain. The frame-zero intermediates are identity (0x10300017/1a) and zero (1b). The active 80-byte
0x10300015 block is neutralised to identity with reciprocal zoom 1. Runtime code supplies actual clip
processing geometry, one matrix per mounted DNG frame, reciprocal crop, and fresh version identities.
Unknown native fields and the Perspective global parameters are retained. The method's Boost text
is intentionally native; its UI settings are not an editable mcstudio configuration channel.

Body: 0x81 + Zstandard; protobuf path 3/9/1/6/2. Global entries are repeated field 3, frame container
field 4. Frame records are repeated field 1, with local zigzag index in field 1 and parameter entries
in field 2. The active Variant's field 5 contains five big-endian uint64 values (2,1280,720,0,0),
then little-endian float32 reciprocal zoom and nine output-to-input matrix values. 1280x720 is the
native cache coordinate space, independent of source and timeline resolution.

Research's CinemaDNG render reproduced 96 authored gyro corrections with median registration error
0.025 pixels. That establishes the matrix route in Resolve Free, not compatibility with every camera,
geometry or Resolve release. The portable numerical regression fixture retains three of those
matrices and their input rotations, without media identities or private paths.
