"""MCP server tests through the SDK's in-process client.

Each test opens its own client: anyio cancel scopes must be entered and
exited in the same task, which a yield fixture cannot guarantee.
"""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult

from lgtv_mcp import config, server
from lgtv_mcp.config import TvEntry
from lgtv_mcp.control import ControllerPool
from lgtv_mcp.discovery import Found

from .conftest import Network
from .fakes import TV_UUID, FakeClient

EXPECTED_TOOLS = {
    "list_tvs",
    "discover_tvs",
    "pair_tv",
    "pair_tv_status",
    "set_tv_address",
    "get_status",
    "get_tv_info",
    "list_apps",
    "list_inputs",
    "power",
    "set_screen",
    "set_volume",
    "set_mute",
    "switch_input",
    "launch_app",
    "play_youtube",
    "press_keys",
    "show_message",
}


@pytest.fixture
def pool(network: Network, cfg_path: Path) -> ControllerPool:
    return ControllerPool(cfg_path, client_factory=FakeClient)


@asynccontextmanager
async def session(pool: ControllerPool) -> AsyncIterator[Client]:
    async with Client(server.create_server(pool)) as client:
        yield client


def text(result: CallToolResult) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


async def call(
    pool: ControllerPool, name: str, args: dict[str, Any] | None = None
) -> CallToolResult:
    async with session(pool) as client:
        return await client.call_tool(name, args or {})


async def test_lists_expected_tools_with_annotations(pool: ControllerPool) -> None:
    async with session(pool) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    for name, tool in tools.items():
        assert tool.description, name
        assert tool.annotations is not None, name
    assert tools["get_status"].annotations.read_only_hint is True
    assert tools["power"].annotations.destructive_hint is True
    assert tools["set_tv_address"].annotations.destructive_hint is True
    assert tools["pair_tv"].annotations.destructive_hint is True  # replace=True drops a key
    assert tools["discover_tvs"].annotations.open_world_hint is True
    assert tools["show_message"].annotations.read_only_hint is False
    for name in ("get_status", "list_apps", "list_inputs", "discover_tvs"):
        assert "not instructions" in (tools[name].description or ""), name


async def test_schema_limits(pool: ControllerPool) -> None:
    async with session(pool) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    volume = tools["set_volume"].input_schema["properties"]["level"]
    assert "maximum" in str(volume)
    message = tools["show_message"].input_schema["properties"]["text"]
    assert message["maxLength"] == 200
    keys = tools["press_keys"].input_schema["properties"]["keys"]
    assert keys["maxItems"] == 20


async def test_no_tv_paired(pool: ControllerPool) -> None:
    result = await call(pool, "get_status")
    assert result.is_error
    assert "No TV paired yet" in text(result)


async def test_status(pool: ControllerPool, paired: TvEntry) -> None:
    result = await call(pool, "get_status")
    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["channel"] == "81-6 La Nacion HD"


async def test_list_tvs_never_exposes_keys(pool: ControllerPool, paired: TvEntry) -> None:
    result = await call(pool, "list_tvs")
    assert "saved-key" not in str(result.structured_content) + text(result)
    assert result.structured_content == {
        "tvs": [{"name": "living", "host": "192.168.4.40", "model": "50UM7360", "default": True}]
    }


async def test_info_never_exposes_key(pool: ControllerPool, paired: TvEntry) -> None:
    result = await call(pool, "get_tv_info")
    assert not result.is_error
    assert "saved-key" not in str(result.structured_content) + text(result)


async def test_discover_marks_paired(
    pool: ControllerPool, paired: TvEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        server,
        "discover",
        lambda: [Found("192.168.4.40", TV_UUID, "WebOS"), Found("192.168.4.41", "x", "WebOS")],
    )
    result = await call(pool, "discover_tvs")
    assert result.structured_content == {
        "tvs": [
            {"host": "192.168.4.40", "server": "WebOS", "paired_as": "living"},
            {"host": "192.168.4.41", "server": "WebOS", "paired_as": None},
        ]
    }


async def test_pairing_flow(pool: ControllerPool, cfg_path: Path) -> None:
    async with session(pool) as client:
        started = await client.call_tool("pair_tv", {"host": "192.168.4.40", "name": "living"})
        assert started.structured_content is not None
        assert started.structured_content["state"] == "waiting"
        state = "waiting"
        for _ in range(100):
            status = await client.call_tool("pair_tv_status", {"name": "living"})
            assert status.structured_content is not None
            state = status.structured_content["state"]
            if state != "waiting":
                break
            await asyncio.sleep(0.01)
    assert state == "paired"
    assert config.load(cfg_path).tvs["living"].key == "new-key"


async def test_pair_rejects_public_host(pool: ControllerPool) -> None:
    result = await call(pool, "pair_tv", {"host": "8.8.8.8", "name": "x"})
    assert result.is_error
    assert "not a local network address" in text(result)


async def test_launch_and_errors(pool: ControllerPool, paired: TvEntry) -> None:
    ok = await call(pool, "launch_app", {"name": "netflix"})
    assert not ok.is_error
    assert "Opened Netflix" in text(ok)
    ambiguous = await call(pool, "launch_app", {"name": "play"})
    assert ambiguous.is_error
    assert "matches several apps" in text(ambiguous)


async def test_youtube(pool: ControllerPool, paired: TvEntry) -> None:
    result = await call(pool, "play_youtube", {"url_or_id": "-8DNbZkeRTs"})
    assert result.structured_content is not None
    assert result.structured_content["target"] == "https://www.youtube.com/tv?v=-8DNbZkeRTs"


async def test_volume_requires_exactly_one_argument(pool: ControllerPool, paired: TvEntry) -> None:
    assert (await call(pool, "set_volume")).is_error
    assert (await call(pool, "set_volume", {"level": 5, "step": "up"})).is_error
    assert not (await call(pool, "set_volume", {"level": 5})).is_error
    assert not (await call(pool, "set_volume", {"step": "down"})).is_error


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("set_volume", {"level": 101}),
        ("set_volume", {"level": -1}),
        ("show_message", {"text": ""}),
        ("show_message", {"text": "x" * 201}),
        ("press_keys", {"keys": []}),
        ("press_keys", {"keys": ["home"] * 21}),
        ("power", {"state": "reboot"}),
        ("get_status", {"tv": "x" * 33}),
        ("set_volume", {"level": True}),
        ("set_volume", {"level": False}),
    ],
)
async def test_schema_validation_rejects(
    pool: ControllerPool, paired: TvEntry, tool: str, args: dict[str, Any]
) -> None:
    assert (await call(pool, tool, args)).is_error
    assert not [c for tv in FakeClient.instances for c in tv.calls if c[0] == "set_volume"]


async def test_host_tools_never_look_up_names(
    pool: ControllerPool, paired: TvEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    looked_up: list[object] = []

    def getaddrinfo(*args: object, **kwargs: object) -> list[object]:
        looked_up.append(args)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    for tool, args in (
        ("pair_tv", {"host": "c2VjcmV0.x7.attacker.example", "name": "x"}),
        ("set_tv_address", {"name": "living", "host": "tv.attacker.example"}),
    ):
        assert (await call(pool, tool, args)).is_error, tool
    assert looked_up == []


async def test_power_off_and_on(pool: ControllerPool, paired: TvEntry, network: Network) -> None:
    off = await call(pool, "power", {"state": "off"})
    assert "Turned the TV off" in text(off)
    network.reachable_hosts = set()
    on = await call(pool, "power", {"state": "on"})
    assert "Wake-on-LAN" in text(on)
    assert network.woken


async def test_unknown_tv_name(pool: ControllerPool, paired: TvEntry) -> None:
    result = await call(pool, "get_status", {"tv": "kitchen"})
    assert result.is_error
    assert "Known TVs: living" in text(result)


async def test_unexpected_errors_are_masked(
    pool: ControllerPool, paired: TvEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_: object) -> None:
        raise RuntimeError("secret internal detail saved-key")

    monkeypatch.setattr(ControllerPool, "load_config", explode)
    result = await call(pool, "list_tvs")
    assert result.is_error
    assert "saved-key" not in text(result)


def test_version_flag(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["lgtv-mcp", "--version"])
    server.main()
    assert capsys.readouterr().out.startswith("lgtv-mcp ")


def test_rejects_unknown_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["lgtv-mcp", "--http"])
    with pytest.raises(SystemExit):
        server.main()


async def test_moved_tv_error_mentions_confirmation(
    pool: ControllerPool, paired: TvEntry, network: Network
) -> None:
    network.reachable_hosts = {"192.168.4.77"}
    network.discovered = [Found("192.168.4.77", TV_UUID, "WebOS")]
    result = await call(pool, "get_status")
    assert result.is_error
    assert "set_tv_address" in text(result)
    assert FakeClient.instances == []


async def test_set_tv_address(
    pool: ControllerPool, paired: TvEntry, network: Network, cfg_path: Path
) -> None:
    network.reachable_hosts = {"192.168.4.77"}
    result = await call(pool, "set_tv_address", {"name": "living", "host": "192.168.4.77"})
    assert not result.is_error
    assert config.load(cfg_path).tvs["living"].host == "192.168.4.77"


async def test_pair_tv_refuses_overwrite_without_replace(
    pool: ControllerPool, paired: TvEntry
) -> None:
    result = await call(pool, "pair_tv", {"host": "192.168.4.40", "name": "living"})
    assert result.is_error
    assert "already paired" in text(result)
