import json
import os
import stat
import sys
from pathlib import Path

import pytest

from lgtv_mcp import config
from lgtv_mcp.config import Config, TvEntry
from lgtv_mcp.errors import ConfigError, InvalidInput, NotConfigured, UnknownTv

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "lgtv" / "config.json"


def entry(host: str = "192.168.1.10", key: str = "secret-key") -> TvEntry:
    return TvEntry(host=host, key=key, uuid="uuid-1", model="50UM7360", macs=["02:ab:cd:00:00:01"])


def test_default_path_uses_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LGTV_CONFIG", str(tmp_path / "x.json"))
    assert config.config_path() == tmp_path / "x.json"


def test_default_path_uses_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LGTV_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if sys.platform != "win32":
        assert config.config_path() == tmp_path / "lgtv" / "config.json"


def test_missing_file_is_empty_config(path: Path) -> None:
    cfg = config.load(path)
    assert cfg.tvs == {}
    assert cfg.default is None


def test_round_trip(path: Path) -> None:
    cfg = Config()
    cfg.add("living", entry(), make_default=True)
    config.save(cfg, path)
    loaded = config.load(path)
    assert loaded.default == "living"
    assert loaded.tvs["living"] == entry()


@posix_only
def test_save_uses_private_permissions(path: Path) -> None:
    cfg = Config()
    cfg.add("living", entry(), make_default=True)
    config.save(cfg, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@posix_only
def test_save_tightens_existing_loose_file(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    path.chmod(0o644)
    config.save(Config(), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@posix_only
def test_load_warns_on_loose_permissions(path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config.save(Config(), path)
    path.chmod(0o644)
    config.load(path)
    assert "readable by other users" in caplog.text


def test_save_leaves_no_temp_files(path: Path) -> None:
    config.save(Config(), path)
    config.save(Config(), path)
    assert [p.name for p in path.parent.iterdir()] == ["config.json"]


def test_invalid_json_raises_config_error(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        config.load(path)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"tvs": []},
        {"tvs": {"living": {"host": 1, "key": "k"}}},
        {"tvs": {"living": {"key": "k"}}},
        {"tvs": {"BAD NAME": {"host": "h", "key": "k"}}},
        {"default": 3, "tvs": {}},
    ],
)
def test_malformed_config_raises(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError):
        config.load(path)


def test_non_utf8_config_raises_config_error(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe{")
    with pytest.raises(ConfigError):
        config.load(path)


def test_deeply_nested_config_raises_config_error(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("[" * 100_000 + "]" * 100_000)
    with pytest.raises(ConfigError):
        config.load(path)


@posix_only
def test_save_writes_through_symlink(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "lgtv.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = tmp_path / "lgtv" / "config.json"
    link.parent.mkdir()
    link.symlink_to(real)
    config.save(Config(), link)
    assert link.is_symlink()
    assert stat.S_IMODE(real.stat().st_mode) == 0o600


def test_dangling_default_is_dropped(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"default": "gone", "tvs": {}}))
    assert config.load(path).default is None


def test_migrates_legacy_files(path: Path) -> None:
    path.parent.mkdir(parents=True)
    (path.parent / "key").write_text("legacy-key\n")
    (path.parent / "host").write_text("192.168.4.40\n")
    cfg = config.load(path)
    assert cfg.default == "tv"
    assert cfg.tvs["tv"].host == "192.168.4.40"
    assert cfg.tvs["tv"].key == "legacy-key"
    assert path.exists()
    assert (path.parent / "key").exists()


def test_no_migration_without_both_legacy_files(path: Path) -> None:
    path.parent.mkdir(parents=True)
    (path.parent / "key").write_text("legacy-key")
    assert config.load(path).tvs == {}


def test_get_uses_default() -> None:
    cfg = Config()
    cfg.add("living", entry(), make_default=True)
    cfg.add("bedroom", entry("192.168.1.11"))
    assert cfg.get(None)[0] == "living"
    assert cfg.get("bedroom")[1].host == "192.168.1.11"


def test_first_tv_becomes_default() -> None:
    cfg = Config()
    cfg.add("living", entry())
    assert cfg.default == "living"


def test_get_errors() -> None:
    cfg = Config()
    with pytest.raises(NotConfigured):
        cfg.get(None)
    cfg.add("living", entry())
    with pytest.raises(UnknownTv, match="Known TVs: living"):
        cfg.get("kitchen")


def test_single_tv_without_default_is_used() -> None:
    cfg = Config(tvs={"only": entry()})
    assert cfg.get(None)[0] == "only"


def test_remove_moves_default() -> None:
    cfg = Config()
    cfg.add("living", entry(), make_default=True)
    cfg.add("bedroom", entry())
    cfg.remove("living")
    assert cfg.default == "bedroom"
    cfg.remove("bedroom")
    assert cfg.default is None


@pytest.mark.parametrize("name", ["", "Living", "a b", "-x", "../etc", "x" * 33, "tv;rm"])
def test_invalid_names(name: str) -> None:
    with pytest.raises(InvalidInput):
        config.validate_name(name)


@pytest.mark.parametrize("name", ["tv", "living-room", "tv_2", "0"])
def test_valid_names(name: str) -> None:
    assert config.validate_name(name) == name


def test_update_is_load_modify_save(path: Path) -> None:
    config.update(lambda c: c.add("living", entry()), path)
    config.update(lambda c: c.add("bedroom", entry("192.168.1.11")), path)
    assert sorted(config.load(path).tvs) == ["bedroom", "living"]


def test_repr_hides_key() -> None:
    assert "secret-key" not in repr(entry())


def test_env_path_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LGTV_CONFIG", "~/custom.json")
    assert config.config_path() == Path(os.path.expanduser("~/custom.json"))
