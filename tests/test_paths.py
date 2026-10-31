from pathlib import Path

import pytest

from mcrawsome.paths import cache_dir, config_dir, data_dir, mount_dir, state_dir


def test_cache_dir_follows_the_override_then_xdg_then_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCRAWSOME_CACHE_DIR", str(tmp_path / "mine"))
    assert cache_dir() == tmp_path / "mine"
    monkeypatch.delenv("MCRAWSOME_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache_dir() == tmp_path / "xdg" / "mcrawsome"
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cache_dir() == tmp_path / "home" / ".cache" / "mcrawsome"


def test_state_dir_follows_the_override_then_xdg_then_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCRAWSOME_STATE_DIR", str(tmp_path / "mine"))
    assert state_dir() == tmp_path / "mine"
    monkeypatch.delenv("MCRAWSOME_STATE_DIR")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert state_dir() == tmp_path / "xdg" / "mcrawsome"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert state_dir() == tmp_path / "home" / ".local" / "state" / "mcrawsome"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in (
        "MCRAWSOME_CACHE_DIR",
        "MCRAWSOME_STATE_DIR",
        "MCRAWSOME_MOUNT_DIR",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_a_new_install_uses_the_new_name(home: Path) -> None:
    assert [cache_dir(), state_dir(), config_dir(), data_dir(), mount_dir()] == [
        home / ".cache/mcrawsome",
        home / ".local/state/mcrawsome",
        home / ".config/mcrawsome",
        home / ".local/share/mcrawsome",
        home / "Mounts/mcrawsome",
    ]
