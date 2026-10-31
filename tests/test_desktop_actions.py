"""Default menus, per-user preferences and explicit repair of a missing system loader."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcrawsome import desktop_actions as module
from mcrawsome.gui import Bridge
from mcrawsome.packaging import packaging_dir


@pytest.fixture
def menu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    executable = tmp_path / "mcrawsome"
    executable.touch()
    executable.chmod(0o700)
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setattr(module, "LAUNCHER", executable)
    monkeypatch.setattr(module, "_adapter", lambda: module.ADAPTERS[0])
    monkeypatch.setattr(module, "_support_available", lambda adapter: True)
    monkeypatch.setattr(module, "_can_install_support", lambda: True)
    extensions = tmp_path / "system"
    monkeypatch.setattr(module, "EXTENSIONS_HOME", extensions)
    target = extensions / module.ADAPTERS[0].extension
    target.parent.mkdir(parents=True)
    target.write_bytes((packaging_dir() / module.ADAPTERS[0].asset).read_bytes())
    return data


def test_menu_defaults_on_and_saves_disable_preference(menu: Path) -> None:
    before = module.menu_status()
    assert before["enabled"]
    assert before["current"]
    assert not menu.exists()
    other = menu / "nautilus-python/extensions/other.py"
    other.parent.mkdir(parents=True)
    other.write_text("# another application\n")
    installed = module.set_menu(True)
    assert installed["enabled"]
    assert installed["current"]
    assert not installed["requires_admin"]
    configuration = json.loads((menu / "mcrawsome/file-manager-actions.json").read_text())
    assert configuration == {"version": 3, "enabled": True}
    assert not module.set_menu(False)["enabled"]
    assert json.loads((menu / "mcrawsome/file-manager-actions.json").read_text()) == {
        "version": 3,
        "enabled": False,
    }
    assert not module.menu_status()["enabled"]
    assert module.set_menu(True)["enabled"]
    assert other.read_text() == "# another application\n"


def test_missing_loader_requires_explicit_administrator_install(
    menu: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "_support_available", lambda adapter: False)
    status = module.menu_status()
    assert status["requires_admin"]
    assert "Administrator" in status["message"]
    with pytest.raises(ValueError, match="Administrator"):
        module.set_menu(True)
    assert not menu.exists()
    called: list[bool] = []

    def install(adapter: module.Adapter) -> None:
        called.append(True)
        monkeypatch.setattr(module, "_support_available", lambda adapter: True)

    monkeypatch.setattr(module, "_install_support", install)
    assert module.set_menu(True, install_support=True)["enabled"]
    assert called == [True]


def test_bridge_decline_does_not_install_or_write(menu: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_support_available", lambda adapter: False)
    bridge = Bridge(18911, [])
    asked: list[str] = []

    def confirm(title: str, question: str) -> bool:
        asked.append(question)
        return False

    bridge._window = SimpleNamespace(create_confirmation_dialog=confirm)
    result = bridge.set_file_menu(True)
    assert result["cancelled"] is True
    assert "Administrator access" in asked[0]
    assert not menu.exists()


def test_package_install_uses_only_fixed_host_commands(menu: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ready = False
    monkeypatch.setattr(module, "_support_available", lambda adapter: ready)
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        nonlocal ready
        calls.append(command)
        assert isinstance(kwargs["env"], dict)
        ready = True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("mcrawsome.desktop_actions.subprocess.run", run)
    module.set_menu(True, install_support=True)
    assert calls == [["/usr/bin/pkexec", "/usr/bin/apt-get", "install", "-y", "python3-nautilus"]]
    module.set_menu(False)
    assert len(calls) == 1


@pytest.mark.parametrize("requires_admin", [True, False])
def test_bridge_installs_system_support_only_after_confirmation_when_needed(
    menu: Path, monkeypatch: pytest.MonkeyPatch, requires_admin: bool
) -> None:
    monkeypatch.setattr(module, "_support_available", lambda adapter: not requires_admin)
    calls: list[tuple[bool, bool]] = []
    questions: list[str] = []

    def confirm(title: str, question: str) -> bool:
        questions.append(question)
        return True

    def install(enabled: bool, *, install_support: bool = False) -> module.MenuStatus:
        calls.append((enabled, install_support))
        return module.menu_status()

    monkeypatch.setattr(module, "set_menu", install)
    bridge = Bridge(18911, [])
    bridge._window = SimpleNamespace(create_confirmation_dialog=confirm)
    assert bridge.set_file_menu(True)["ok"] is True
    assert calls == [(True, requires_admin)]
    assert len(questions) == (1 if requires_admin else 0)


def test_relative_xdg_data_home_uses_the_personal_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", "relative/folder")
    assert module.data_home() == tmp_path / ".local/share"


def test_configuration_for_another_executable_is_not_enabled(menu: Path) -> None:
    module.set_menu(True)
    (menu / "mcrawsome/file-manager-actions.json").write_text(
        json.dumps({"version": 2, "executable": "/tmp/other-program"})
    )
    assert not module.menu_status()["enabled"]


def test_file_association_uses_personal_xdg_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(command)
        assert kwargs["check"] is True
        return SimpleNamespace(stdout="mcrawsome.desktop\n")

    monkeypatch.setattr("mcrawsome.desktop_actions.subprocess.run", run)
    assert module.associate_files()
    assert calls == [
        ["/usr/bin/xdg-mime", "default", "mcrawsome.desktop", "video/x-motioncam-raw"],
        ["/usr/bin/xdg-mime", "query", "default", "video/x-motioncam-raw"],
    ]
