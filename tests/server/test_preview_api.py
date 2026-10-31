"""The preview WebSocket and the clip's sound (WS /clips/{id}/preview, GET /clips/{id}/audio)."""

import io
import multiprocessing
import subprocess
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from mcrawsome.paths import cache_dir

API = "/api/v1"
STEP = 41_666_667  # ns between frames at 24 fps
SAME_ORIGIN = {"origin": "http://127.0.0.1:8642"}
SET_EXPOSURE = {"develop": {"exposure": 0.0}}  # automatic exposure would send the frame again once measured


@pytest.mark.parametrize("value", [-1, True, 1.5, "3"])
def test_playback_preparation_frame_is_validated(value: object) -> None:
    from mcrawsome.errors import McError
    from mcrawsome.server.routes.preview import _request

    with pytest.raises(McError, match="playback preparation"):
        _request({"op": "frame", "seq": 1, "index": 0, "warm_from": value})


def test_playback_preparation_frame_is_forwarded() -> None:
    from mcrawsome.server.routes.preview import _request

    assert _request({"op": "frame", "seq": 1, "index": 0, "warm_from": 25})["warm_from"] == 25


def test_an_unidentified_video_opens_after_saving_its_source_profile(
    client: TestClient, library: Path
) -> None:
    from mcrawsome.output.ffmpeg import find_ffmpeg

    video = library / "unidentified.mp4"
    subprocess.run(
        [
            str(find_ffmpeg()[0]),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x96:r=24",
            "-frames:v",
            "3",
            "-c:v",
            "mpeg4",
            str(video),
        ],
        check=True,
    )
    with client.websocket_connect(_url(video), headers=SAME_ORIGIN) as ws:
        error = ws.receive_json()
        assert error["kind"] == "error"
        assert "colour space and transfer function" in error["error"]["message"]
    video.with_suffix(".mcrawsome.toml").write_text(
        '[develop]\ninput_gamut = "rec709"\ninput_transfer = "bt709"\n'
    )
    with client.websocket_connect(_url(video), headers=SAME_ORIGIN) as ws:
        assert ws.receive_json()["kind"] == "opened"
        ws.send_json(
            {
                "seq": 1,
                "op": "recipe",
                "index": 0,
                "recipe": {
                    "develop": {
                        "input_gamut": "rec709",
                        "input_transfer": "bt709",
                        "exposure": 0,
                    }
                },
            }
        )
        replies = _replies(ws, lambda header: header["kind"] in ("refined", "error"))
    assert replies[-1][0]["kind"] == "refined"
    assert replies[-1][1].startswith(b"\xff\xd8")


@pytest.fixture
def clip(make_mcraw: Callable[..., Path], library: Path) -> Path:
    return make_mcraw(library / "clip.mcraw", [i * STEP for i in range(6)])


def _url(clip: Path) -> str:
    return f"ws://127.0.0.1:8642{API}/clips/local/preview?path={clip}"  # the Host this server answers to


def _replies(ws: Any, until: Callable[[dict[str, Any]], bool]) -> list[tuple[dict[str, Any], bytes]]:
    """Headers (with the binary frame each announces) until one satisfies `until`."""
    got = []
    while True:
        header = ws.receive_json()
        payload = ws.receive_bytes() if header["bytes"] else b""
        got.append((header, payload))
        if until(header):
            return got
        assert header["kind"] != "error", header


def test_a_frame_request_gets_a_draft_then_a_refined_frame(client: TestClient, clip: Path) -> None:
    with client.websocket_connect(_url(clip), headers=SAME_ORIGIN) as ws:
        opened = ws.receive_json()
        assert opened["kind"] == "opened"
        assert (opened["frame_count"], opened["width"], opened["height"]) == (6, 64, 32)
        ws.send_json({"seq": 1, "op": "recipe", "index": 2, "recipe": SET_EXPOSURE})
        replies = _replies(ws, lambda header: header["kind"] == "refined")
    assert [(header["seq"], header["kind"], header["index"]) for header, _ in replies] == [
        (1, "draft", 2),
        (1, "refined", 2),
    ]
    for header, jpeg in replies:
        assert jpeg[:2] == b"\xff\xd8"
        assert header["bytes"] == len(jpeg)
        assert header["ms"] >= 0
    assert replies[1][0]["notes"] == [
        "not stabilised: no gyro log (clip.gcsv) next to the clip, and no motion recorded in it"
    ]


def test_the_preview_width_can_be_chosen(client: TestClient, clip: Path) -> None:
    with client.websocket_connect(_url(clip) + "&width=32") as ws:
        opened = ws.receive_json()
    assert (opened["width"], opened["height"]) == (32, 16)
    with client.websocket_connect(_url(clip) + "&width=8") as ws:
        error = ws.receive_json()
    assert error["error"]["code"] == "recipe-invalid"


def test_a_burst_of_requests_gets_one_draft(client: TestClient, clip: Path) -> None:
    with client.websocket_connect(_url(clip)) as ws:
        ws.receive_json()
        ws.send_json({"seq": 1, "op": "recipe", "index": 0, "recipe": SET_EXPOSURE})
        for seq in range(2, 6):
            ws.send_json({"seq": seq, "op": "frame", "index": seq})
        replies = _replies(ws, lambda header: header["kind"] == "refined")
    assert [(header["seq"], header["kind"]) for header, _ in replies] == [(5, "draft"), (5, "refined")]


def test_a_recipe_layer_changes_the_frame(client: TestClient, clip: Path) -> None:
    with client.websocket_connect(_url(clip)) as ws:
        ws.receive_json()
        ws.send_json({"seq": 1, "op": "frame", "index": 1})
        _replies(ws, lambda header: header["kind"] == "draft")
        ws.send_json({"seq": 2, "op": "recipe", "recipe": {"develop": {"exposure": 1.5}, "look": "gritty"}})
        [(header, _)] = _replies(ws, lambda header: header["kind"] == "draft")
        assert header["exposure"]["stops"] == 1.5
        ws.send_json({"seq": 3, "op": "recipe", "recipe": {"develop": {"exposre": 1}}})
        [(error, _)] = _replies(ws, lambda header: header["kind"] == "error")
    assert error["seq"] == 3
    assert error["error"]["code"] == "recipe-invalid"
    assert "develop.exposre" in error["error"]["message"]


def test_a_malformed_message_is_answered_with_an_error(client: TestClient, clip: Path) -> None:
    with client.websocket_connect(_url(clip)) as ws:
        ws.receive_json()
        ws.send_json({"seq": "one", "op": "frame"})
        [(error, _)] = _replies(ws, lambda header: header["kind"] == "error")
        assert error["error"]["code"] == "recipe-invalid"
        ws.send_json({"seq": 2, "op": "frame", "index": 0})  # the session goes on
        _replies(ws, lambda header: header["kind"] == "draft")


def test_a_foreign_origin_is_refused(client: TestClient, clip: Path) -> None:
    with (
        pytest.raises(WebSocketDenialResponse) as refused,
        client.websocket_connect(_url(clip), headers={"origin": "http://evil.example"}),
    ):
        pass
    assert refused.value.status_code == 403
    assert refused.value.json()["error"]["code"] == "origin-not-allowed"
    assert multiprocessing.active_children() == []


def test_a_clip_outside_the_library_is_refused(client: TestClient, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.mcraw"
    outside.write_bytes(b"x")
    with client.websocket_connect(_url(outside)) as ws:
        error = ws.receive_json()
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert error["kind"] == "error"
    assert error["error"]["code"] == "path-not-allowed"


def test_the_preview_process_ends_with_its_websocket(client: TestClient, clip: Path) -> None:
    with client.websocket_connect(_url(clip)) as ws:
        ws.receive_json()
        assert len(multiprocessing.active_children()) == 1
    deadline = time.monotonic() + 20  # the server closes it once it sees the client go
    while multiprocessing.active_children() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert multiprocessing.active_children() == []


def test_opening_another_clip_closes_the_first(
    client: TestClient, clip: Path, make_mcraw: Callable[..., Path], library: Path
) -> None:
    other = make_mcraw(library / "other.mcraw", [i * STEP for i in range(3)])
    with client.websocket_connect(_url(clip)) as first:
        first.receive_json()
        with client.websocket_connect(_url(other)) as second:
            assert second.receive_json()["frame_count"] == 3
            closed = first.receive_json()
            assert closed["kind"] == "closed"
            assert len(multiprocessing.active_children()) == 1
            second.send_json({"seq": 1, "op": "frame", "index": 2})
            _replies(second, lambda header: header["kind"] == "draft")


def test_the_sound_is_a_wav_as_long_as_the_frames(
    client: TestClient, make_mcraw: Callable[..., Path], library: Path
) -> None:
    frames = 30
    tone = (np.sin(np.arange(int(48000 * 1.4)) * 0.05) * 8000).astype(np.int16)
    clip = make_mcraw(
        library / "sound.mcraw", [i * STEP for i in range(frames)], audio=np.stack([tone, tone], 1)
    )
    response = client.get(f"{API}/clips/local/audio", params={"path": str(clip)})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(response.content)) as sound:
        assert (sound.getsampwidth(), sound.getnchannels(), sound.getframerate()) == (2, 2, 48000)
        seconds = sound.getnframes() / sound.getframerate()
    assert abs(seconds - frames / 24) <= 1 / 24
    assert not (cache_dir() / "audio").exists()
    again = client.get(f"{API}/clips/local/audio", params={"path": str(clip)})
    assert again.content == response.content
    assert not (cache_dir() / "audio").exists()  # served directly from the recording


def test_the_sound_of_a_clip_outside_the_library_is_refused(client: TestClient, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.mcraw"
    outside.write_bytes(b"x")
    response = client.get(f"{API}/clips/local/audio", params={"path": str(outside)})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path-not-allowed"


def test_playback_sends_a_small_draft(client: TestClient, clip: Path) -> None:
    with client.websocket_connect(_url(clip)) as ws:
        ws.receive_json()
        ws.send_json({"seq": 1, "op": "frame", "index": 0, "playback": True})
        header, jpeg = _replies(ws, lambda header: header["kind"] == "draft")[-1]
        assert header["playback"] is True
        assert jpeg[:2] == b"\xff\xd8"


def test_new_preview_does_not_wait_for_old_background_work(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import threading

    from mcrawsome.preview.session import PreviewSettings
    from mcrawsome.recipe.schema import Recipe
    from mcrawsome.server.routes import preview

    release = threading.Event()
    closing = threading.Event()

    class Worker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def close(self) -> None:
            closing.set()
            assert release.wait(5)

    monkeypatch.setattr(preview, "PreviewWorker", Worker)

    async def check() -> None:
        host = preview.PreviewHost()
        first = await host.open(Path("first"), Recipe(), PreviewSettings())
        try:
            second = await asyncio.wait_for(host.open(Path("second"), Recipe(), PreviewSettings()), 0.5)
            assert second is not first
            assert closing.wait(2)
            assert not release.is_set()
        finally:
            release.set()
            host.close()
            await asyncio.sleep(0.05)

    asyncio.run(check())


def test_disconnecting_while_preview_opens_releases_its_worker(
    client: TestClient, clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from starlette.websockets import WebSocket

    from mcrawsome.preview.worker import PreviewWorker
    from mcrawsome.server.routes.preview import PreviewHost

    released: list[PreviewWorker] = []
    sent = threading.Event()
    closed = threading.Event()
    original = PreviewHost.release

    async def release(host: PreviewHost, worker: PreviewWorker) -> None:
        await original(host, worker)
        released.append(worker)
        closed.set()

    async def disconnected(socket: WebSocket, data: Any, mode: str = "text") -> None:
        sent.set()
        raise WebSocketDisconnect(1006)

    monkeypatch.setattr(PreviewHost, "release", release)
    monkeypatch.setattr(WebSocket, "send_json", disconnected)
    with client.websocket_connect(_url(clip)):
        assert sent.wait(5)
        assert closed.wait(5)
    assert len(released) == 1
    assert not released[0].alive


def test_fast_and_bounded_lookahead_are_forwarded(client: TestClient, clip: Path) -> None:
    import cv2

    with client.websocket_connect(_url(clip)) as ws:
        ws.receive_json()
        ws.send_json(
            {
                "seq": 1,
                "op": "recipe",
                "recipe": SET_EXPOSURE,
                "index": 1,
                "playback": True,
                "ahead": 3,
            }
        )
        replies = _replies(ws, lambda header: header.get("buffer_end") is True)
        assert [header["index"] for header, _ in replies] == [1, 2, 3]
        for header, payload in replies:
            assert "after_bytes" not in header
            assert cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR) is not None
        ws.send_json({"seq": 2, "op": "refine", "index": 2})
        [(header, payload)] = _replies(ws, lambda header: header["kind"] == "refined")
        assert header["index"] == 2
        assert payload[:2] == b"\xff\xd8"
        ws.send_json({"seq": 3, "op": "frame", "index": 0, "ahead": 4})
        [(error, _)] = _replies(ws, lambda header: header["kind"] == "error")
        assert error["error"]["code"] == "recipe-invalid"


def test_about_reports_the_running_preview_backend(client: TestClient, clip: Path) -> None:
    client.put(f"{API}/settings", json={"preview_gpu": False})
    with client.websocket_connect(_url(clip) + "&fast_width=1920") as ws:
        ws.receive_json()
        ws.send_json({"seq": 1, "op": "recipe", "index": 1, "fast": True, "recipe": SET_EXPOSURE})
        replies = _replies(ws, lambda header: header["kind"] == "draft")
        header = replies[-1][0]
        assert header["backend"] == "CPU"
        assert header["backend_reason"] == "disabled by setting"
        assert header["timings"]["jpeg_ms"] >= 0
        checks = {check["name"]: check for check in client.get(f"{API}/about").json()["checks"]}
        assert checks["active preview backend"]["detail"] == "CPU (disabled by setting)"


@pytest.mark.timeout(60)
def test_source_stream_uses_clock_credits_and_can_be_replaced(client: TestClient, clip: Path) -> None:
    # Verify transport on CPU; optional real-device kernel tests cover OpenCL separately.
    assert client.put(f"{API}/settings", json={"preview_gpu": False}).status_code == 200
    with client.websocket_connect(_url(clip), headers=SAME_ORIGIN) as ws:
        opened = ws.receive_json()
        assert opened["supports_source"] is True
        ws.send_json(
            {"seq": 1, "op": "recipe", "recipe": SET_EXPOSURE, "index": 0, "source": True, "playback": True}
        )
        # Supply clock credit as soon as a picture arrives, as the playing interface does.
        # A cold first picture can increase the lead before warm pictures reduce the window.
        first = _replies(ws, lambda header: header.get("index", -1) >= 0)
        indices = [header["index"] for header, _ in first]
        assert indices[0] == 0
        assert indices == sorted(set(indices))
        assert all(header.get("stream") and header.get("source") for header, _ in first)
        assert all((header["width"], header["height"]) == (128, 64) for header, _ in first)
        if indices[-1] < 5:
            ws.send_json({"seq": 2, "op": "advance", "stream_seq": 1, "index": 3})
            rest = _replies(ws, lambda header: header.get("index", -1) >= 5)
            later = [header["index"] for header, _ in rest]
            assert later == sorted(set(later))
            assert later[0] > indices[-1]
            assert all(header["seq"] == 1 for header, _ in rest)
        ws.send_json({"seq": 3, "op": "frame", "index": 1, "source": True})
        [(header, _)] = _replies(ws, lambda header: header.get("seq") == 3)
        assert (header["width"], header["height"]) == (128, 64)
        assert header.get("stream") is not True


@pytest.mark.timeout(60)
def test_adaptive_playback_streams_drafts_on_the_audio_clock(client: TestClient, clip: Path) -> None:
    # Verify transport on CPU; optional real-device kernel tests cover OpenCL separately.
    assert client.put(f"{API}/settings", json={"preview_gpu": False}).status_code == 200
    with client.websocket_connect(_url(clip), headers=SAME_ORIGIN) as ws:
        opened = ws.receive_json()
        assert opened["supports_stream"] is True
        ws.send_json(
            {"seq": 1, "op": "recipe", "recipe": SET_EXPOSURE, "index": 0, "stream": True, "playback": True}
        )
        first = _replies(ws, lambda header: header.get("index", -1) >= 0)
        indices = [header["index"] for header, _ in first]
        assert indices[0] == 0
        assert indices == sorted(set(indices))
        assert all(header.get("stream") and not header.get("source") for header, _ in first)
        assert all(header["playback"] and "buffer_end" not in header for header, _ in first)
        if indices[-1] < 5:  # slow processing can already have advanced to the final picture
            ws.send_json({"seq": 2, "op": "advance", "stream_seq": 1, "index": 3})
            rest = _replies(ws, lambda header: header.get("index", -1) >= 5)
            assert rest[0][0]["index"] > indices[-1]
            assert all(header["seq"] == 1 for header, _ in rest)
        ws.send_json({"seq": 3, "op": "frame", "index": 1})  # a pause or seek ends the stream
        [(header, _)] = _replies(ws, lambda header: header.get("seq") == 3)
        assert header.get("stream") is not True


@pytest.mark.parametrize(
    "invalid",
    [{}, {"stream_seq": True, "index": 0}, {"stream_seq": 0, "index": 0}, {"stream_seq": 1, "index": -1}],
)
def test_source_clock_credits_are_validated(invalid: dict[str, Any]) -> None:
    from mcrawsome.errors import McError
    from mcrawsome.server.routes.preview import _request

    with pytest.raises(McError, match="clock"):
        _request({"seq": 2, "op": "advance", **invalid})


@pytest.mark.timeout(60)
def test_native_playback_sends_leases_and_release_does_not_cancel_the_stream(
    client: TestClient, make_mcraw: Callable[..., Path], library: Path
) -> None:
    from mcrawsome.preview.display import DisplayLease, DisplayReader

    assert client.put(f"{API}/settings", json={"preview_gpu": False}).status_code == 200

    # Under load the producer can jump ahead by four frames after each clock update.
    clip = make_mcraw(library / "native.mcraw", [i * STEP for i in range(24)])
    names: set[str] = set()
    last = -1
    with client.websocket_connect(_url(clip), headers=SAME_ORIGIN) as ws:
        ws.receive_json()
        ws.send_json(
            {
                "seq": 1,
                "op": "recipe",
                "index": 0,
                "recipe": SET_EXPOSURE,
                "source": True,
                "playback": True,
                "native": True,
            }
        )
        for index in range(3):
            header = ws.receive_json()
            assert header["bytes"] == 0
            assert header["index"] > last
            last = header["index"]
            assert header["seq"] == 1
            lease = DisplayLease.model_validate(header["display"])
            names.add(lease.name)
            with DisplayReader(lease) as reader:
                pixels = reader.pixels(lease)
                assert pixels.shape == (64, 128, 4)
                del pixels
            ws.send_json({"seq": 2 * index + 2, "op": "release", "display": lease.model_dump()})
            # A release returns a display slot, not playback clock credit. The page sends both.
            ws.send_json({"seq": 2 * index + 3, "op": "advance", "stream_seq": 1, "index": last + 1})
        header = ws.receive_json()
        assert header["index"] > last
        assert header["seq"] == 1
        assert header["stream"]
        # Scrubbing switches to ordinary images while retiring the native producer.
        ws.send_json({"seq": 8, "op": "frame", "index": 2, "fast": True, "scrub": True})
        header, payload = _replies(ws, lambda h: h.get("seq") == 8)[-1]
        assert not header.get("display")
        assert payload.startswith(b"\xff\xd8")
    deadline = time.monotonic() + 5
    while any((Path("/dev/shm") / name).exists() for name in names) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert all(not (Path("/dev/shm") / name).exists() for name in names)


@pytest.mark.parametrize("display", [None, {}, {"name": "../outside"}, {"slot": -1}, "file"])
def test_native_release_rejects_malformed_leases(display: object) -> None:
    from mcrawsome.errors import McError
    from mcrawsome.server.routes.preview import _request

    with pytest.raises(McError, match="validation error"):
        _request({"seq": 3, "op": "release", "display": display})


@pytest.mark.parametrize("span", ["bytes=0-43", "bytes=50-89", "bytes=-12", "bytes=80-"])
def test_raw_preview_audio_supports_byte_seeking_without_extracting(
    client: TestClient,
    make_mcraw: Callable[..., Path],
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
    span: str,
) -> None:
    from mcrawsome.io.clip import Clip

    clip = make_mcraw(
        library / "sound.mcraw",
        [i * STEP for i in range(3)],
        audio=np.arange(8000, dtype=np.int16).reshape(-1, 2),
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("audio must stream without reading whole PCM or any RAW pixels")

    monkeypatch.setattr(Clip, "audio", forbidden)
    monkeypatch.setattr(Clip, "raw_frame", forbidden)
    url = f"{API}/clips/local/audio"
    complete = client.get(url, params={"path": str(clip)})
    partial = client.get(url, params={"path": str(clip)}, headers={"Range": span})
    assert partial.status_code == 206
    assert partial.headers["accept-ranges"] == "bytes"
    start, end = map(int, partial.headers["content-range"].split()[1].split("/")[0].split("-"))
    assert partial.content == complete.content[start : end + 1]
    assert int(partial.headers["content-length"]) == len(partial.content)


def test_the_trim_bar_gets_the_clips_sound_as_peaks(
    client: TestClient, make_mcraw: Callable[..., Path], library: Path
) -> None:
    from mcrawsome.preview.waveform import POINTS

    sound = np.full((9600, 2), -8192, np.int16)  # a quarter of full scale, longer than the three frames
    clip = make_mcraw(library / "sound.mcraw", [i * STEP for i in range(3)], audio=sound)
    response = client.get(f"{API}/clips/local/waveform", params={"path": str(clip)})
    assert response.status_code == 200, response.text
    peaks = response.json()["peaks"]
    assert len(peaks) == POINTS
    assert min(peaks) == pytest.approx(0.25, abs=0.001)


@pytest.mark.parametrize(
    "span", ["bytes=999999999-", "bytes=30-10", "bytes=-0", "bytes=0-4,10-20", "other=0-4"]
)
def test_raw_audio_rejects_invalid_ranges(
    client: TestClient, make_mcraw: Callable[..., Path], library: Path, span: str
) -> None:
    clip = make_mcraw(library / "sound.mcraw", [0, STEP], audio=np.zeros((800, 2), np.int16))
    response = client.get(f"{API}/clips/local/audio", params={"path": str(clip)}, headers={"Range": span})
    assert response.status_code == 416
    assert response.headers["content-range"].startswith("bytes */")
