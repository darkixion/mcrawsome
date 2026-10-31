import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from mcrawsome.cli.main import cli
from mcrawsome.doctor import Check, run_checks

NAMES = {
    "Python",
    "MCRAWsome",
    "native extension",
    "FFmpeg",
    "ffprobe",
    "H.265 decoder",
    "ProRes decoder",
    "AV1 decoder",
    "camera profiles",
    "personal defaults",
    "memory",
    "CPU",
    "CPU temperature",
    "free space here",
    "free space in the state folder",
    "job store",
}


def _doctor(*args: str) -> tuple[int, list[dict[str, Any]]]:
    result = CliRunner().invoke(cli, ["doctor", *args, "--json"])
    return result.exit_code, json.loads(result.output)["checks"]


def test_doctor_reports_every_check(fake_ffmpeg: Path) -> None:
    code, checks = _doctor()
    names = {c["name"] for c in checks}
    assert names >= NAMES
    assert not any(n.startswith("encoder ") for n in names)
    assert code == (1 if any(c["status"] == "fail" for c in checks) else 0)


def test_doctor_reports_gpu_video_decoding(fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.io import vaapi

    differ = "the GPU's frames differ from FFmpeg's own decoder by up to 70 codes"

    def trial(ffmpeg: Path, codec: str, device: str) -> str | None:
        return None if codec == "hevc" else differ

    monkeypatch.setattr(vaapi, "_trial", trial)
    monkeypatch.setenv("MCRAWSOME_VAAPI_DECODE", "1")
    vaapi._REMEMBERED.clear()
    _, checks = _doctor()
    by_name = {c["name"]: c for c in checks}
    assert by_name["decoder gpu hevc"]["status"] == "ok"
    assert by_name["decoder gpu hevc"]["detail"] == "H.265 Direct Log videos decode on /dev/dri/renderD128"
    av1 = by_name["decoder gpu av1"]
    assert av1["status"] == "warn"
    assert av1["detail"] == f"AV1: {differ}"
    assert "processor" in av1["fix"]


def test_doctor_names_an_nvidia_gpu_that_decodes(fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.io import vaapi

    monkeypatch.setattr(vaapi, "_trial", lambda ffmpeg, codec, device: None if device == "cuda:0" else "no")
    monkeypatch.setattr(vaapi, "_nvidia_devices", lambda: ["cuda:0"])
    monkeypatch.setenv("MCRAWSOME_VAAPI_DECODE", "1")
    vaapi._REMEMBERED.clear()
    _, checks = _doctor()
    hevc = next(c for c in checks if c["name"] == "decoder gpu hevc")
    assert (hevc["status"], hevc["detail"]) == ("ok", "H.265 Direct Log videos decode on NVIDIA GPU 0")


def test_doctor_without_ffmpeg_fails_with_the_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.delenv("MCRAWSOME_FFMPEG", raising=False)
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("MCRAWSOME_MKVMERGE", raising=False)
    code, checks = _doctor()
    assert code == 0  # FFmpeg is optional for RAW
    by_name = {c["name"]: c for c in checks}
    assert by_name["FFmpeg"]["status"] == "warn"
    assert "install" in by_name["FFmpeg"]["fix"].lower()
    assert not any(n.startswith("encoder ") for n in by_name)


def test_refresh_probes_again(fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    def probe(ffmpeg: Path, refresh: bool = False) -> list[Any]:
        calls.append(refresh)
        return []

    monkeypatch.setattr("mcrawsome.io.vaapi.probe_decoders", probe)
    _doctor()
    _doctor("--refresh")
    assert calls == [False, True]


def test_broken_defaults_are_reported_and_retired_looks_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    (config / "mcrawsome" / "looks").mkdir(parents=True)
    (config / "mcrawsome" / "looks" / "bad.toml").write_text("this is not toml [")
    (config / "mcrawsome" / "defaults.toml").write_text("[develop]\nexposure = 999\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    by_name = {c.name: c for c in run_checks()}
    assert "looks" not in by_name
    assert by_name["personal defaults"].status == "fail"
    assert by_name["personal defaults"].fix is not None


def test_an_unwritable_state_folder_fails_the_job_store_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("MCRAWSOME_STATE_DIR", str(blocker / "state"))
    check = next(c for c in run_checks() if c.name == "job store")
    assert isinstance(check, Check)
    assert check.status == "fail"
    ok = tmp_path / "fine"
    monkeypatch.setenv("MCRAWSOME_STATE_DIR", str(ok))
    assert next(c for c in run_checks() if c.name == "job store").status == "ok"
    assert not ok.exists()  # checking creates nothing


def test_doctor_prints_a_table(fake_ffmpeg: Path) -> None:
    result = CliRunner().invoke(cli, ["doctor"])
    assert "AV1 decoder" in result.output
    assert "decoder gpu av1" in result.output


def test_the_cpu_temperature_check_reads_the_sensor_or_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sensor = tmp_path / "temp"
    sensor.write_text("52000\n")
    monkeypatch.setattr("mcrawsome.doctor.find_cpu_sensor", lambda: sensor)
    check = {c.name: c for c in run_checks()}["CPU temperature"]
    assert check.status == "ok"
    assert check.detail == f"52 C ({sensor})"
    monkeypatch.setattr("mcrawsome.doctor.find_cpu_sensor", lambda: None)
    check = {c.name: c for c in run_checks()}["CPU temperature"]
    assert check.status == "warn"
    assert check.fix is not None


def test_preview_gpu_missing_driver_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome import doctor

    def missing() -> Any:
        raise OSError("no vendor ICD")

    monkeypatch.setenv("MCRAWSOME_PREVIEW_GPU", "1")
    monkeypatch.setattr("mcrawsome.preview.backend.OpenCL", missing)
    check = doctor._preview_compute()
    assert check.status == "warn"
    assert "CPU" in check.detail
    assert "no vendor ICD" in check.detail


def test_preview_gpu_can_be_disabled_in_saved_settings() -> None:
    from mcrawsome import doctor
    from mcrawsome.server.settings import InterfaceSettings, save_settings

    save_settings(InterfaceSettings(preview_gpu=False))
    check = doctor._preview_compute()
    assert check.status == "ok"
    assert "disabled" in check.detail
    assert "CPU" in check.detail


def test_doctor_says_gpu_video_decoding_is_off_unless_switched_on(
    fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.io import vaapi

    monkeypatch.setattr(vaapi, "_trial", lambda *_: None)
    monkeypatch.delenv("MCRAWSOME_VAAPI_DECODE", raising=False)
    vaapi._REMEMBERED.clear()
    _, checks = _doctor()
    hevc = next(c for c in checks if c["name"] == "decoder gpu hevc")
    assert hevc["status"] == "ok"
    assert hevc["detail"] == (
        "H.265: decodes like FFmpeg's own decoder on /dev/dri/renderD128; not used, as it was no faster "
        "here (MCRAWSOME_VAAPI_DECODE=1 uses it)"
    )
