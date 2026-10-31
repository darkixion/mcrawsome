import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcrawsome import support
from mcrawsome.paths import state_dir


def test_support_report_is_local_on_demand_redacted_and_bounded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(support, "run_checks", list)
    monkeypatch.setattr(support, "system_info", lambda: {"home": str(Path.home())})
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / "server.log").write_text("old\n" * 100_000 + f"engine {Path.home()}/example\n")
    (state_dir() / "server.log.1").write_text("previous engine\n")
    remote = client.post("/api/v1/support/report")
    assert remote.status_code == 403
    with TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 50000)) as local:
        response = local.post("/api/v1/support/report")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    files = response.json()["files"]
    assert {"system.json", "doctor.json", "server.log", "server.log.1"} <= files.keys()
    assert files["server.log"].endswith("engine ~/example\n")
    assert str(Path.home()) not in response.text
    assert all(len(text.encode()) <= 64 * 1024 for text in files.values())
    assert len(json.dumps(response.json()).encode()) < 8 * 1024**2
    assert not any(name.startswith(("clips/", "sidecars/")) for name in files)


def test_support_report_without_engine_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(support, "run_checks", list)
    monkeypatch.setattr(support, "system_info", dict)
    assert support.diagnostic_members() == {"doctor.json": '{\n  "checks": []\n}\n', "system.json": "{}\n"}


def test_log_tail_never_reads_an_entire_large_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class BoundedReader(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            assert size is not None
            assert 0 < size <= 128
            return super().read(size)

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: BoundedReader(b"old\n" * 1000 + b"last\n"))
    text = support._read_tail(tmp_path / "log", 128)
    assert text.startswith(support._CUT)
    assert text.endswith("last\n")
    assert len(text.encode()) <= 128
