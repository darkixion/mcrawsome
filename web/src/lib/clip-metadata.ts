import type { ClipDetails } from "./library";
import { metadataHelp } from "./metadata";
import { MOVEMENT_LABELS } from "./library-filters";

export type MetadataFact = { name: string; value: string; help: string; path?: boolean };
export type MetadataSection = { name: string; facts: MetadataFact[] };
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const short = (value: number, places = 1) => Number(value.toFixed(places));

export function recordedMetadata(clip: ClipDetails): { sections: MetadataSection[]; calibration: Record<string, unknown> } {
  const container = object(clip.metadata?.Container), first = object(clip.metadata?.["First frame"]);
  const video = object(clip.metadata?.Video), device = object(container.deviceSpecificProfile);
  const colourTags = object(video.tags);
  const telemetry = object(clip.metadata?.Telemetry), logDevice = object(clip.metadata?.Device);
  const extra = object(container.extraData), settings = object(extra.postProcessSettings), about = object(settings.metadata);
  function section(name: string, values: [string, unknown, boolean?][]): MetadataSection {
    return { name, facts: values.filter(([,value]) => value !== undefined && value !== null && value !== "")
      .map(([name,value,path]) => ({ name, value: String(value), help: metadataHelp(name), path })) };
  }
  const audioRate = extra.audioSampleRate ?? video.audio_rate;
  const audioChannels = extra.audioChannels ?? video.audio_channels;
  const shutter = clip.exposure_time_ns && clip.exposure_time_ns > 0 ? clip.exposure_time_ns < 1e9
    ? `1/${short(1e9 / clip.exposure_time_ns, 2)} s` : `${short(clip.exposure_time_ns / 1e9, 3)} s` : undefined;
  const captured = clip.captured ? new Date(clip.captured).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : undefined;
  const lat = settings.gpsLatitude, lon = settings.gpsLongitude, alt = settings.gpsAltitude;
  const gpsTime = typeof settings.gpsTime === "string" ? settings.gpsTime.trim() : "";
  const gps = typeof lat === "number" && Number.isFinite(lat) && Math.abs(lat) <= 90
    && typeof lon === "number" && Number.isFinite(lon) && Math.abs(lon) <= 180 && (lat !== 0 || lon !== 0 || gpsTime !== "");
  const sections = [
    section("Camera & lens", [
      ["Camera", clip.camera ?? device.deviceModel], ["Manufacturer", about["build.manufacturer"]],
      ["Lens", clip.focal_length_mm != null ? `${short(clip.focal_length_mm)} mm (35 mm equivalent)` : undefined], ["Camera ID", device.cameraId],
    ]),
    section("Exposure", [
      ["Aperture", clip.aperture != null ? `f/${short(clip.aperture)}` : undefined], ["ISO", clip.iso], ["Shutter", shutter],
      ["Focus distance", typeof first.focusDistance === "number" ? `${short(first.focusDistance, 3)} dioptres` : undefined],
    ]),
    section("Recording", [
      ["Recorded frame", clip.width != null && clip.height != null ? `${clip.width} x ${clip.height}` : undefined],
      ["Captured", captured], ["Measured rate", clip.measured_fps != null ? `${clip.measured_fps.toFixed(3)} fps` : undefined],
      ["Missing frames", clip.missing_frames],
      ["Movement", clip.movement ? MOVEMENT_LABELS[clip.movement] ?? clip.movement : undefined],
      ["Shake blur", clip.shake_blur_px != null ? `Most frames under ${short(clip.shake_blur_px)} px${clip.blurred_s ? `; ${short(clip.blurred_s)} s sustained` : ""}` : undefined],
      ["Codec", video.codec], ["Pixel format", video.pix_fmt],
      ["Colour range", video.colour_range === "pc" ? "Full" : video.colour_range === "tv" ? "Video (limited)" : video.colour_range],
      ["Frame timing", typeof video.variable_frame_rate === "boolean" ? video.variable_frame_rate ? "Variable (fixed-rate preview and export)" : "Constant" : undefined],
      ["Colour space", video.colour_space], ["Transfer function", video.transfer_function],
      ["Colour source", video.encoding_source],
      ["Container primaries", colourTags.primaries], ["Container transfer", colourTags.transfer],
      ["YCbCr matrix", colourTags.matrix], ["Colour note", video.colour_note],
    ]),
    section("RAW data", [
      ["Bayer pattern", typeof container.sensorArrangment === "string" ? container.sensorArrangment.toUpperCase() : undefined],
      ["White level", clip.white_level], ["Black levels", clip.black_levels?.join(", ")],
      ["Sensor dimensions", first.originalWidth != null && first.originalHeight != null ? `${first.originalWidth} x ${first.originalHeight}` : undefined],
    ]),
    section("Audio", [
      ["Sample rate", typeof audioRate === "number" && audioRate > 0 ? `${short(audioRate / 1000, 3)} kHz` : undefined],
      ["Channels", audioChannels],
    ]),
    section("Telemetry", [
      ["Gyro samples", telemetry.gyro_samples],
      ["Accelerometer", typeof telemetry.accelerometer === "boolean" ? telemetry.accelerometer ? "Recorded" : "Not recorded" : undefined],
      ["Telemetry clock", telemetry.clock], ["Gyro axes", telemetry.axes],
      ["Telemetry span", typeof telemetry.start_s === "number" && typeof telemetry.end_s === "number" ? `${short(telemetry.start_s, 3)} to ${short(telemetry.end_s, 3)} s` : undefined],
      ["Phone stabilisation (EIS)", logDevice.EISA === "N" ? "Off" : logDevice.EISA === "Y" ? "On" : undefined],
    ]),
    section("GPS", [
      ["GPS latitude", gps ? `${lat.toFixed(7)}°` : undefined],
      ["GPS longitude", gps ? `${lon.toFixed(7)}°` : undefined],
      ["GPS altitude", gps && typeof alt === "number" && Number.isFinite(alt) ? `${short(alt, 3)} m` : undefined],
      ["GPS time", gps ? gpsTime : undefined],
      ["Place", clip.place ? `${clip.place}${clip.place_km != null && clip.place_km > 10 ? ` (about ${Math.round(clip.place_km)} km away)` : ""}` : undefined],
    ]),
    section("File", [["Source", clip.path, true], ...clip.paths?.filter(path => path !== clip.path).map(path => ["Also available", path, true] as [string, unknown, boolean]) ?? []]),
  ].filter(group => group.facts.length > 0);
  const calibration = Object.fromEntries([
    ...Object.entries(container).filter(([key]) => /^(color|forward|calibration)Matrix|^colorIlluminant/.test(key)),
    ...Object.entries(first).filter(([key]) => /^(asShotNeutral|lensShadingMap|noiseProfile)/.test(key)),
  ]);
  return { sections, calibration };
}
