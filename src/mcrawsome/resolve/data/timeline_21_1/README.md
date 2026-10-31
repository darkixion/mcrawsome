# Native timeline scaffold

Resolve 21.1.1.0010, database schema 17. Three XML documents for a 24 fps timeline with one video and
one stereo audio track. The media descriptors come from mcstudio's own sanitised synthetic bin
scaffold. The timeline and Fairlight structure were measured from the owner's native timeline export;
its media descriptors, paths, image thumbnail and cached metadata were discarded. Source properties
were replaced with a generated three-frame 256 x 144 DNG sequence and synthetic PCM WAV, then all
non-zero identifiers were replaced. No recording, thumbnail or pixel/audio data is included.

SequenceSetup and the neutral sequence colour-version geometry retain Resolve's native structure.
The Fairlight model retains the native single stereo strip, rather than inventing mixer objects.
Timeline items have no colour-version override: RAW defaults come from the shared Media Pool
Radiometry descriptor. The supplied export did not have the per-item white-balance version claimed
in the external research report. Reserved zero identifiers are retained.

Before product integration, the owner imported, played and rendered a six-second synthetic two-clip
candidate and confirmed linked video/audio selection and movement. A second candidate with stereo
followed by mono on the same audio track also passed playback, pairing and rendering. Reciprocal
partner references use FieldsBlob outer1/inner1/field1; the start frame remains in field21. These are
timeline links. No unmeasured Media Pool audio-link field is added.

The writer uses fresh identifiers for each send and pair, preserves one audio track, and leaves a gap
for a silent source. It refuses mixed or fractional frame rates rather than guessing a conformation.
Compatibility with older Resolve versions and additional track layouts is unverified.
