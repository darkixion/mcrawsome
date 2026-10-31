"""Desktop activations reuse one owner, queue before page load and preserve literal file paths."""

import json
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcrawsome import gui
from mcrawsome.gui_instance import GuiInstance


def test_activation_queues_before_the_window_is_ready_and_recovers_after_close(tmp_path: Path) -> None:
    owner = GuiInstance.acquire(18901, [])
    assert owner is not None
    path = tmp_path / '--browser "quote" $() %f 雪.mcraw'
    scripts: list[str] = []
    delivered = threading.Event()
    shown = threading.Event()

    def evaluate(script: str) -> None:
        scripts.append(script)
        delivered.set()

    try:
        assert GuiInstance.acquire(18901, [path]) is None
        assert not delivered.is_set()
        owner.attach(SimpleNamespace(evaluate_js=evaluate, restore=lambda: None, show=shown.set))
        assert delivered.wait(2)
        assert json.dumps([str(path)]) in scripts[0]
        assert "mcrawsome-open-files" in scripts[0]
        assert shown.wait(2)
    finally:
        owner.close()
    replacement = GuiInstance.acquire(18901, [])
    assert replacement is not None
    replacement.close()


def test_other_process_forwards_all_files_and_plain_launch_only_activates(tmp_path: Path) -> None:
    owner = GuiInstance.acquire(18902, [])
    assert owner is not None
    scripts: list[str] = []
    raised: list[bool] = []
    done = threading.Event()

    def show() -> None:
        raised.append(True)
        if len(raised) == 2:
            done.set()

    owner.attach(SimpleNamespace(evaluate_js=scripts.append, restore=lambda: None, show=show))
    paths = [tmp_path / "one.mcraw", tmp_path / "two %.MCRAW"]
    code = (
        "from pathlib import Path; import sys; from mcrawsome.gui_instance import GuiInstance; "
        "assert GuiInstance.acquire(18902, [Path(p) for p in sys.argv[1:]]) is None"
    )
    try:
        for files in (paths, []):
            subprocess.run([sys.executable, "-c", code, *map(str, files)], check=True, timeout=5)
        assert done.wait(2)
        assert len(scripts) == 1
        assert json.dumps(list(map(str, paths))) in scripts[0]
    finally:
        owner.close()


def test_launch_forwards_before_any_engine_or_window_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = GuiInstance.acquire(18903, [])
    assert owner is not None
    done = threading.Event()
    owner.attach(
        SimpleNamespace(evaluate_js=lambda script: done.set(), restore=lambda: None, show=lambda: None)
    )

    def fail(*args: object, **kwargs: object) -> None:
        pytest.fail("forwarding touched the engine or created a window")

    monkeypatch.setattr(gui, "answers", fail)
    monkeypatch.setattr(gui, "show", fail)
    try:
        gui.launch(18903, [], lambda message: None, clips=(tmp_path / "one.mcraw", tmp_path / "two.mcraw"))
        assert done.wait(2)
    finally:
        owner.close()


@pytest.mark.parametrize("payload", [b"not json\n", b'["relative.mcraw"]\n', b"[null]\n", b"{}\n"])
def test_invalid_activation_is_rejected_without_losing_the_owner(payload: bytes) -> None:
    owner = GuiInstance.acquire(18904, [])
    assert owner is not None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(owner.listener.getsockname())
            client.sendall(payload)
            assert client.recv(32) == b""
        assert owner.pending.empty()
        assert GuiInstance.acquire(18904, []) is None
    finally:
        owner.close()
