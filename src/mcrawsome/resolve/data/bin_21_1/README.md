# Native bin scaffold

Resolve 21.1.1.0010, database schema 17. Derived from mcstudio's own synthetic Bin Probe A export,
using three generated flat DNG frames (256 x 144 at 24 fps) and a generated stereo PCM WAV.
No recording, thumbnail or pixel data is included. The XML and opaque settings are Resolve-generated;
no third-party authoring code is included.

All source paths, source names, project names, workstation identifiers, dates and UUIDs were replaced.
Cached per-media metadata blobs were cleared. Nested project settings were decompressed and their
private paths replaced with synthetic placeholders while retaining field lengths. Versioned envelope
payloads use Resolve's uncompressed flag. The radiometry retains its native direct zstd envelope.

The production writer clones the audio/video pool elements, generates fresh non-zero UUIDs, preserves
zero sentinels, and replaces each descriptor using the mounted source snapshot. It clones the matching
empty metadata lock objects with their owner references. It does not infer unsupported rotations or
multichannel audio. Compatibility with older Resolve versions is unverified.
