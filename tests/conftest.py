from collections.abc import Iterator
from pathlib import Path

import pytest

from lgtv_mcp import config, control
from lgtv_mcp.config import Config, TvEntry
from lgtv_mcp.discovery import Found

from .fakes import TV_UUID, FakeClient


class Network:
    """Controls what the fake local network looks like to the code under test."""

    def __init__(self) -> None:
        self.reachable_hosts: set[str] = {"192.168.4.40"}
        self.discovered: list[Found] = []
        self.woken: list[tuple[list[str], str | None]] = []

    def reachable(self, host: str, *_: object, **__: object) -> bool:
        return host in self.reachable_hosts

    def discover(self, *_: object, **__: object) -> list[Found]:
        return self.discovered

    def wake_on_lan(self, macs: list[str], host: str | None = None) -> None:
        self.woken.append((macs, host))


@pytest.fixture(autouse=True)
def _reset_fake() -> Iterator[None]:
    FakeClient.reset()
    yield
    FakeClient.reset()


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> Network:
    net = Network()
    monkeypatch.setattr(control, "reachable", net.reachable)
    monkeypatch.setattr(control, "discover", net.discover)
    monkeypatch.setattr(control, "wake_on_lan", net.wake_on_lan)
    return net


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "lgtv" / "config.json"
    monkeypatch.setenv("LGTV_CONFIG", str(path))
    return path


@pytest.fixture
def paired(cfg_path: Path) -> TvEntry:
    entry = TvEntry(
        host="192.168.4.40",
        key="saved-key",
        uuid=TV_UUID,
        model="50UM7360",
        macs=["02:ab:cd:00:00:01", "02:ab:cd:00:00:02"],
    )
    cfg = Config()
    cfg.add("living", entry, make_default=True)
    config.save(cfg, cfg_path)
    return entry
