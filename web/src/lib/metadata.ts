export function metadataLabel(key: string): string {
  const text = key.replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/[_.]+/g, " ");
  const labelled = text.replace(/\bColor\b/g, "Colour").replace(/\bArrangment\b/g, "Arrangement").replace(/\bIso\b/g, "ISO");
  return labelled.charAt(0).toUpperCase() + labelled.slice(1);
}
export function metadataValue(value: unknown): string {
  if (value === null || value === undefined) return "Not recorded";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

const help: Record<string, string> = {
  "Camera": "Camera model recorded by the source device.",
  "Manufacturer": "Manufacturer reported by the recording device.",
  "Lens": "Reported focal length as a 35 mm equivalent, not the physical lens focal length.",
  "Camera ID": "Android camera identifier. This identifies a phone camera module, not your production Camera label.",
  "Aperture": "Recorded lens aperture as an f-number. A smaller number means a wider aperture.",
  "ISO": "Recorded sensor sensitivity for the first frame. This is capture metadata, not an output adjustment.",
  "Shutter": "Exposure duration of the first recorded frame, in seconds.",
  "Focus distance": "Recorded focus setting in dioptres (inverse metres). Zero denotes infinity; the estimate may be approximate.",
  "Captured": "First frame receive time, displayed in your local timezone. Resolve receives Date Recorded in UTC.",
  "Recorded frame": "Frame dimensions stored in the recording. A larger sensor area does not mean the file contains that many pixels.",
  "Measured rate": "Frame rate measured from source timestamps. This can differ from the chosen output frame rate.",
  "Missing frames": "Number of gaps identified in the recording's frame sequence.",
  "Shake blur": "Motion blur from shake, estimated from the gyro log, the shutter and the focal length: 95% of frames stay under this many pixels. Sustained means 20 pixels or more for half a second or longer. Stabilisation cannot remove blur; a faster shutter avoids it.",
  "Movement": "How the phone moved for most of the clip, measured from its gyro and accelerometer log: Still, Pan or tilt (a steady turn), Moving (carried while walking or travelling) or Handheld. A measurement, not a judgement of the shot.",
  "Codec": "Compression codec reported by the source video stream.",
  "Pixel format": "Recorded video sample layout and bit depth, as reported by the decoder.",
  "Colour range": "Full range uses all code values (0 to 1023 at 10-bit). Video range uses 64 to 940 for 10-bit luma. The recorded flag is respected automatically; range is separate from LOG colour encoding.",
  "Frame timing": "Variable-rate LOG pictures follow their recorded presentation times on a fixed-rate preview and export clock. Choose another output frame rate to repeat or skip frames without changing audio speed.",
  "Colour space": "The colour space (gamut) inferred from MotionCam's file name, or identified by supported metadata. Colour source tells you which. Verify renamed files and override the input settings in Resolve Settings if needed. sRGB and BT.709 share primaries.",
  "Transfer function": "The gamma or log curve inferred from the file name or supported metadata. DaVinci Intermediate is the transfer function; DaVinci Wide Gamut is the colour space. Input overrides are in Resolve Settings.",
  "Colour source": "How the input profile was identified. File name means inferred, rather than embedded recording metadata. MotionCam can write generic container tags for LOG or linear recordings, so those tags cannot establish its profile. Check renamed files and use explicit Develop settings when needed.",
  "Container primaries": "The primaries reported by the container or codec. These can be generic or incorrect for MotionCam's recording profile, and are shown separately from the identified input colour space.",
  "Container transfer": "The transfer reported by the container or codec. iec61966-2-1 means sRGB. MotionCam can report it for LOG or linear recordings, so it cannot establish their recording profile.",
  "YCbCr matrix": "How encoded luma and chroma values convert to RGB. A BT.709 matrix can carry RGB values in a different gamut and transfer function; it does not identify either one.",
  "Colour note": "Why an input colour profile needs an explicit choice. This does not affect the separately recorded full/video signal range or YCbCr matrix.",
  "Gyro samples": "Recorded angular-velocity samples from embedded GPMF telemetry. Used by Stabilise and gyro analytics.",
  "Gyro axes": "Device axes after applying the recorded telemetry orientation.",
  "Accelerometer": "Recorded acceleration samples, separate from the gyro's angular velocity.",
  "Bayer pattern": "Arrangement of red, green and blue samples in the recorded RAW sensor mosaic.",
  "White level": "Recorded RAW saturation level. Values near this limit represent clipped sensor highlights.",
  "Black levels": "Recorded RAW sensor offsets for the four Bayer channels, before development adjustments.",
  "Sensor dimensions": "Original sensor area in pixels. The recorded frame may be cropped or binned from this area.",
  "Sample rate": "Recorded audio samples per second. 48 kHz means 48,000 samples per second.",
  "Channels": "Number of recorded audio channels; one is mono and two is stereo.",
  "Source": "Original recording path. Keep this source available while Resolve uses its mounted virtual media.",
  "Also available": "Another known path to the same recording.",
  "Duration": "Recording duration based on its source timing.",
  "Frames": "Number of recorded video frames.",
  "Sensor depth": "Bit depth of the recorded RAW sensor values, before development or output encoding.",
  "GPS latitude": "Recorded latitude in decimal degrees: north is positive and south is negative. Sent to Resolve in Camera Notes.",
  "GPS longitude": "Recorded longitude in decimal degrees: east is positive and west is negative. Sent to Resolve in Camera Notes.",
  "GPS altitude": "Recorded GPS altitude in metres. Its height datum is not established by the container, so the value is preserved as recorded.",
  "Place": "The nearest city to the recorded GPS position, within 50 km, from the GNOME location database installed with the system. Found offline; nothing is sent anywhere. The Notes tab can use it as the Location.",
  "GPS time": "GPS timestamp exactly as recorded by MotionCam. MCRAWsome does not infer a timezone or replace the frame capture time with it.",
  "Title": "Your display name for the clip. Resolve receives this name; original recording and media filenames stay unchanged.",
  "Description": "A short description of what happens in the clip. Sent to Resolve's Description field.",
  "Keywords": "Search terms separated by commas. Resolve receives individual keywords; avoid quotes and line breaks within a term.",
  "Scene": "Your production scene identifier, for example 12B. Sent to Resolve's Scene field.",
  "Shot": "Your shot identifier within the scene. Sent to Resolve's Shot field.",
  "Take": "Your take identifier. Text such as 14A is allowed and is sent to Resolve's Take field.",
  "Location": "Your production location description. Sent to Resolve's Location field, separately from recorded GPS coordinates.",
  "Camera label": "Your production camera label, for example A or B. Sent to Resolve's Camera # field; distinct from the phone model and Android camera ID.",
  "Tag": "Your take selection: Untagged, Good Take or Rejected. Sent to Resolve's Tag field.",
  "Notes": "Your free-form production notes. Sent to Resolve's Comments field.",
};

const aliases: Record<string, string> = {
  gpsLatitude: "GPS latitude", gpsLongitude: "GPS longitude", gpsAltitude: "GPS altitude", gpsTime: "GPS time",
  deviceModel: "Camera", "build.model": "Camera", "build.manufacturer": "Manufacturer", cameraId: "Camera ID",
  iso: "ISO", exposureTime: "Shutter", focusDistance: "Focus distance", recvdTimestampMs: "Captured",
  sensorArrangment: "Bayer pattern", whiteLevel: "White level", dynamicWhiteLevel: "White level",
  blackLevel: "Black levels", dynamicBlackLevel: "Black levels", audioSampleRate: "Sample rate", audioChannels: "Channels",
};

export function metadataHelp(key: string): string {
  if (help[key]) return help[key];
  const alias = aliases[key], description = alias ? help[alias] : undefined;
  if (description) return description;
  if (/^(color|calibration|forward)Matrix[12]$/.test(key)) return "Recorded 3×3 colour calibration matrix for one reference illuminant. Used for RAW colour conversion; it does not describe geometric lens distortion.";
  if (/^colorIlluminant[12]$/.test(key)) return "Reference light source associated with the corresponding recorded colour calibration matrix.";
  if (key === "asShotNeutral") return "Recorded neutral colour response used to establish the capture white balance.";
  if (/^lensShadingMap/.test(key)) return "Recorded lens-shading gain calibration or its grid dimensions. This corrects brightness variation, not geometric lens distortion.";
  if (/^noiseProfile/.test(key)) return "Recorded sensor noise-model coefficients used to describe signal-dependent noise.";
  if (key === "sensorOrientation" || key === "orientation") return "Orientation reported by the recording device. MCRAWsome's saved display turn is a separate setting.";
  if (key === "originalWidth" || key === "originalHeight") return "Original sensor-area dimension in pixels, before the recorded crop or binning.";
  if (key === "width" || key === "height") return "Recorded frame dimension in pixels, before MCRAWsome's saved display rotation.";
  if (key === "focalLengths") return help.Lens!;
  if (key === "apertures") return "Available recorded f-numbers. Multiple values do not establish which aperture was active.";
  if (key === "timestamp") return "Recorded frame timestamp. Its source clock is used for frame timing and gyro synchronisation.";
  if (key === "extraData" || key === "postProcessSettings") return "Additional metadata saved by MotionCam. These recorded settings are separate from MCRAWsome's current development recipe.";
  return `Recorded source field: ${key}. The value is shown as stored in the recording; it is not a current MCRAWsome adjustment.`;
}
