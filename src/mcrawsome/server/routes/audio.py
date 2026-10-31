"""Bounded, seekable RAW preview audio without extracting the whole recording first."""

from collections.abc import Iterator
from pathlib import Path

from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from mcrawsome.errors import McError
from mcrawsome.io.clip import Clip
from mcrawsome.mount.audio import WaveView
from mcrawsome.output.export import clip_sources
from mcrawsome.output.ffmpeg import frame_rate


def raw_audio(path: Path, byte_range: str | None) -> Response:
    stream = path.open("rb")
    try:
        with Clip(path, metadata_only=True, quad_bayer_remosaic=False) as clip:
            if not clip.audio_chunk_count or not clip.metadata.audio_sample_rate:
                raise McError(f"{path.name} has no sound", code="input-missing")
            rate = float(frame_rate(clip.fps))
            view = WaveView(clip, stream.fileno(), len(clip_sources(clip, True)), rate, rate / clip.fps)
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
        first, last, status = 0, view.size - 1, 200
        if byte_range is not None:
            try:
                unit, span = byte_range.split("=", 1)
                begin, end = span.split("-", 1)
                if unit != "bytes" or not (begin or end) or "," in span:
                    raise ValueError
                if begin:
                    first = int(begin)
                    last = min(int(end), last) if end else last
                else:
                    first = max(0, view.size - int(end))
                if first < 0 or first > last or first >= view.size or (not begin and int(end) <= 0):
                    raise ValueError
            except ValueError:
                stream.close()
                return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{view.size}"})
            status = 206
            headers["Content-Range"] = f"bytes {first}-{last}/{view.size}"
        headers["Content-Length"] = str(last - first + 1)

        def chunks() -> Iterator[bytes]:
            try:
                for offset in range(first, last + 1, 64 * 1024):
                    yield view.read(offset, min(64 * 1024, last + 1 - offset))
            finally:
                stream.close()

        return StreamingResponse(
            chunks(),
            status_code=status,
            headers=headers,
            media_type="audio/wav",
            background=BackgroundTask(stream.close),
        )
    except BaseException:
        stream.close()
        raise
