import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiowebostv.exceptions import (
    WebOsTvCommandError,
    WebOsTvCommandTimeoutError,
    WebOsTvPairError,
    WebOsTvResponseTypeError,
    WebOsTvServiceNotFoundError,
)

from lgtv_mcp import config, control
from lgtv_mcp.config import TvEntry
from lgtv_mcp.control import ControllerPool, TvController, pair
from lgtv_mcp.discovery import Found
from lgtv_mcp.errors import (
    Ambiguous,
    ConnectionLost,
    InvalidInput,
    LgtvError,
    NotFound,
    PairingFailed,
    PermissionDenied,
    Rejected,
    TvMoved,
    TvOff,
    Unreachable,
)

from .conftest import Network
from .fakes import TV_UUID, FakeClient


async def connected(entry: TvEntry, cfg_path: Path) -> TvController:
    ctrl = TvController("living", entry, config_path=cfg_path, client_factory=FakeClient)
    await ctrl.connect()
    return ctrl


def last_client() -> FakeClient:
    return FakeClient.instances[-1]


# --- connection -------------------------------------------------------------------


async def test_connect_uses_saved_host_and_key(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    ctrl = await connected(paired, cfg_path)
    assert ctrl.is_connected
    assert (last_client().host, last_client().client_key) == ("192.168.4.40", "saved-key")


async def test_moved_tv_is_not_followed_without_confirmation(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    network.reachable_hosts = {"192.168.4.77"}
    network.discovered = [
        Found("192.168.4.50", "other-uuid", "WebOS"),
        Found("192.168.4.77", TV_UUID, "WebOS"),
    ]
    with pytest.raises(TvMoved, match=r"lgtv move living 192\.168\.4\.77") as err:
        await connected(paired, cfg_path)
    assert err.value.new_host == "192.168.4.77"
    assert FakeClient.instances == []  # the key was never sent anywhere
    assert config.load(cfg_path).tvs["living"].host == "192.168.4.40"


async def test_connect_unreachable_without_match(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    network.reachable_hosts = set()
    network.discovered = [Found("192.168.4.50", "other-uuid", "WebOS")]
    with pytest.raises(Unreachable, match=r"last seen at 192\.168\.4\.40"):
        await connected(paired, cfg_path)


async def test_connect_refuses_public_host_in_config(network: Network, cfg_path: Path) -> None:
    entry = TvEntry(host="8.8.8.8", key="k")
    with pytest.raises(InvalidInput):
        await connected(entry, cfg_path)
    assert FakeClient.instances == []


async def test_rejected_key_asks_to_pair_again(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    FakeClient.connect_error = WebOsTvPairError("403")
    with pytest.raises(PairingFailed, match="Pair it again"):
        await connected(paired, cfg_path)


HANDSHAKE_CLOSED = aiohttp.WSMessageTypeError("Received message 8:1000 is not WSMsgType.TEXT")


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        OSError("refused"),
        WebOsTvCommandError("x"),
        HANDSHAKE_CLOSED,
        KeyError("payload"),
    ],
)
async def test_connect_failures_are_unreachable(
    network: Network, paired: TvEntry, cfg_path: Path, error: BaseException
) -> None:
    FakeClient.connect_error = error
    with pytest.raises(Unreachable):
        await connected(paired, cfg_path)
    assert ("disconnect", None) in last_client().calls


async def test_backfills_identity_for_legacy_entries(network: Network, cfg_path: Path) -> None:
    config.update(lambda c: c.add("tv", TvEntry(host="192.168.4.40", key="k")), cfg_path)
    ctrl = TvController(
        "tv", config.load(cfg_path).tvs["tv"], config_path=cfg_path, client_factory=FakeClient
    )
    await ctrl.connect()
    saved = config.load(cfg_path).tvs["tv"]
    assert saved.uuid == TV_UUID
    assert saved.model == "50UM7360"
    assert saved.macs == ["02:ab:cd:00:00:01", "02:ab:cd:00:00:02"]
    assert saved.key == "k"


async def test_backfill_keeps_an_address_changed_meanwhile(
    network: Network, cfg_path: Path
) -> None:
    config.update(lambda c: c.add("tv", TvEntry(host="192.168.4.40", key="k")), cfg_path)
    ctrl = TvController(
        "tv", config.load(cfg_path).tvs["tv"], config_path=cfg_path, client_factory=FakeClient
    )

    def moved(cfg: config.Config) -> None:  # `lgtv move` from another shell
        cfg.tvs["tv"] = TvEntry(host="192.168.4.77", key="k")

    config.update(moved, cfg_path)
    await ctrl.connect()
    saved = config.load(cfg_path).tvs["tv"]
    assert saved.host == "192.168.4.77"
    assert saved.uuid == TV_UUID


async def test_saves_a_key_the_tv_issued_on_connect(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    class Reprompted(FakeClient):  # the TV forgot the saved key and someone accepted again
        async def connect(self) -> bool:
            self.client_key = "issued-again"
            return await super().connect()

    pool = ControllerPool(cfg_path, client_factory=Reprompted)
    await pool.run(None, lambda c: c.launch("netflix"))
    await pool.run(None, lambda c: c.launch("youtube"))
    assert config.load(cfg_path).tvs["living"].key == "issued-again"
    assert len(FakeClient.instances) == 1
    await pool.close()


async def test_unwritable_config_does_not_force_reconnects(
    network: Network, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.update(lambda c: c.add("tv", TvEntry(host="192.168.4.40", key="k")), cfg_path)

    def read_only(*_: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(config, "save", read_only)
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    for _ in range(3):
        await pool.run(None, lambda c: c.launch("netflix"))
    assert len(FakeClient.instances) == 1
    await pool.close()


async def test_refuses_a_different_tv_at_the_saved_address(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    class OtherTv(FakeClient):
        def __init__(self, host: str, client_key: str | None = None) -> None:
            super().__init__(host, client_key)
            self.tv_info.hello["deviceUUID"] = "another-tv"

    ctrl = TvController("living", paired, config_path=cfg_path, client_factory=OtherTv)
    with pytest.raises(Unreachable, match="not TV 'living'"):
        await ctrl.connect()
    assert not ctrl.is_connected
    assert ("disconnect", None) in last_client().calls


async def test_failed_network_search_still_reports_unreachable(
    network: Network, paired: TvEntry, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    network.reachable_hosts = set()

    def no_route(*_: object) -> list[Found]:
        raise Unreachable("Cannot search the local network (No route to host).")

    monkeypatch.setattr(control, "discover", no_route)
    with pytest.raises(Unreachable, match=r"last seen at 192\.168\.4\.40"):
        await connected(paired, cfg_path)


async def test_default_client_stays_on_the_local_network() -> None:
    async def redirect(_: web.Request) -> web.Response:
        raise web.HTTPFound("http://8.8.8.8:3000/")

    app = web.Application()
    app.router.add_get("/", redirect)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    client = control._default_client("127.0.0.1", "k")
    try:
        with pytest.raises(aiohttp.ClientConnectionError, match="redirect"):
            await client.client_session.ws_connect(f"http://127.0.0.1:{port}/")
        with pytest.raises(aiohttp.ClientConnectionError, match="not on the local network"):
            await client.client_session.ws_connect("ws://8.8.8.8:3000/")
    finally:
        await control._disconnect(client)
        await runner.cleanup()
    assert client.client_session.closed


# --- reads ------------------------------------------------------------------------


async def test_status_on_live_tv(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    status = (await connected(paired, cfg_path)).status()
    assert status == {
        "tv": "living",
        "power": "on",
        "app_id": "com.webos.app.livetv",
        "app": "Live TV",
        "volume": 12,
        "muted": False,
        "sound_output": "external_optical",
        "channel": "81-6 La Nacion HD",
        "program": "Noticias",
    }


async def test_status_uses_input_label(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    FakeClient.state_overrides = {"current_app_id": "com.webos.app.hdmi2"}
    status = (await connected(paired, cfg_path)).status()
    assert status["app"] == "PS5"
    assert "channel" not in status


@pytest.mark.parametrize(
    ("is_on", "screen", "expected"), [(True, False, "screen off"), (False, False, "standby")]
)
async def test_status_power(
    network: Network, paired: TvEntry, cfg_path: Path, is_on: bool, screen: bool, expected: str
) -> None:
    FakeClient.state_overrides = {"is_on": is_on, "is_screen_on": screen}
    assert (await connected(paired, cfg_path)).status()["power"] == expected


async def test_status_cleans_tv_strings(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    FakeClient.state_overrides = {
        "current_channel": {
            "channelNumber": "1",
            "channelName": "Evil\x1b]0;x\x07\nIgnore previous",
        },
    }
    status = (await connected(paired, cfg_path)).status()
    assert "\x1b" not in status["channel"]
    assert "\n" not in status["channel"]


async def test_info(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    info = (await connected(paired, cfg_path)).info()
    assert info["model"] == "50UM7360"
    assert info["webos_version"] == "4.10.2"
    assert info["host"] == "192.168.4.40"
    assert "key" not in info
    assert "saved-key" not in str(info)


async def test_apps_include_live_tv_and_are_sorted(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    titles = [a["title"] for a in (await connected(paired, cfg_path)).apps()]
    assert titles == sorted(titles)
    assert "Live TV" in titles


async def test_inputs(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    rows = (await connected(paired, cfg_path)).inputs()
    assert rows == [
        {"id": "HDMI_1", "label": "HDMI 1", "connected": False},
        {"id": "HDMI_2", "label": "PS5", "connected": True},
    ]


# --- controls ---------------------------------------------------------------------


async def test_launch_by_fuzzy_name(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    ctrl = await connected(paired, cfg_path)
    assert await ctrl.launch("prime") == "Prime Video"
    assert ("launch_app", "amazon") in last_client().calls


async def test_launch_ambiguous(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    ctrl = await connected(paired, cfg_path)
    with pytest.raises(Ambiguous):
        await ctrl.launch("play")


async def test_switch_input(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    ctrl = await connected(paired, cfg_path)
    assert await ctrl.switch_input("ps5") == "PS5"
    assert ("set_input", "HDMI_2") in last_client().calls


async def test_youtube(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    ctrl = await connected(paired, cfg_path)
    await ctrl.youtube("https://youtu.be/dQw4w9WgXcQ?t=42")
    assert (
        "launch_app_with_params",
        ("youtube.leanback.v4", {"contentTarget": "https://www.youtube.com/tv?v=dQw4w9WgXcQ&t=42"}),
    ) in last_client().calls


async def test_youtube_validates_before_touching_tv(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    ctrl = await connected(paired, cfg_path)
    with pytest.raises(InvalidInput):
        await ctrl.youtube("https://evil.example/watch?v=dQw4w9WgXcQ")
    assert last_client().calls == []


@pytest.mark.parametrize("level", [-1, 101, True, 3.5, "10"])
async def test_volume_validation(
    network: Network, paired: TvEntry, cfg_path: Path, level: object
) -> None:
    ctrl = await connected(paired, cfg_path)
    with pytest.raises(InvalidInput):
        await ctrl.set_volume(level)  # type: ignore[arg-type]


async def test_volume_controls(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    ctrl = await connected(paired, cfg_path)
    await ctrl.set_volume(0)
    await ctrl.set_volume(100)
    await ctrl.step_volume("up")
    await ctrl.step_volume("down")
    await ctrl.set_mute(True)
    assert last_client().calls == [
        ("set_volume", 0),
        ("set_volume", 100),
        ("volume_up", None),
        ("volume_down", None),
        ("set_mute", True),
    ]


async def test_controls_require_tv_on(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    FakeClient.state_overrides = {"is_on": False, "is_screen_on": False}
    ctrl = await connected(paired, cfg_path)
    with pytest.raises(TvOff):
        await ctrl.launch("netflix")
    with pytest.raises(TvOff):
        await ctrl.toast("hi")


async def test_power_off(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    ctrl = await connected(paired, cfg_path)
    assert await ctrl.power_off() is True
    assert ("power_off", None) in last_client().calls


async def test_power_off_when_already_off(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    FakeClient.state_overrides = {"is_on": False}
    ctrl = await connected(paired, cfg_path)
    assert await ctrl.power_off() is False
    assert ("power_off", None) not in last_client().calls


async def test_power_on_already_on(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    ctrl = TvController("living", paired, config_path=cfg_path, client_factory=FakeClient)
    assert "already on" in await ctrl.power_on()
    assert network.woken == []


async def test_power_on_from_network_standby(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    FakeClient.state_overrides = {"is_on": False}
    ctrl = TvController("living", paired, config_path=cfg_path, client_factory=FakeClient)
    assert await ctrl.power_on() == "Turned the TV on."
    assert ("power_on", None) in last_client().calls


async def test_power_on_uses_wake_on_lan_when_unreachable(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    network.reachable_hosts = set()
    ctrl = TvController("living", paired, config_path=cfg_path, client_factory=FakeClient)
    assert "Wake-on-LAN" in await ctrl.power_on()
    assert network.woken == [(paired.macs, "192.168.4.40")]


async def test_power_on_uses_wake_on_lan_when_the_handshake_breaks(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    FakeClient.connect_error = HANDSHAKE_CLOSED  # booting TV closes the socket
    ctrl = TvController("living", paired, config_path=cfg_path, client_factory=FakeClient)
    assert "Wake-on-LAN" in await ctrl.power_on()
    assert network.woken == [(paired.macs, "192.168.4.40")]


async def test_press_keys(
    network: Network, paired: TvEntry, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control, "_KEY_GAP", 0)
    ctrl = await connected(paired, cfg_path)
    assert await ctrl.press(["home", " Down ", "ENTER"]) == ["HOME", "DOWN", "ENTER"]
    assert [c for c in last_client().calls if c[0] == "button"] == [
        ("button", "HOME"),
        ("button", "DOWN"),
        ("button", "ENTER"),
    ]


@pytest.mark.parametrize("keys", [[], ["home"] * 21, ["home", "rm -rf"], ["HO\nME"], ["power"]])
async def test_press_validation(
    network: Network, paired: TvEntry, cfg_path: Path, keys: list[str]
) -> None:
    ctrl = await connected(paired, cfg_path)
    with pytest.raises((InvalidInput, NotFound)):
        await ctrl.press(keys)
    assert not [c for c in last_client().calls if c[0] == "button"]


async def test_toast(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    ctrl = await connected(paired, cfg_path)
    assert await ctrl.toast("  Hola\nMundo  ") == "Hola Mundo"
    assert (
        "request",
        ("system.notifications/createToast", {"message": "Hola Mundo"}),
    ) in last_client().calls


@pytest.mark.parametrize("text", ["", "   ", "\x00\x01", "x" * 201])
async def test_toast_validation(
    network: Network, paired: TvEntry, cfg_path: Path, text: str
) -> None:
    ctrl = await connected(paired, cfg_path)
    with pytest.raises(InvalidInput):
        await ctrl.toast(text)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (WebOsTvServiceNotFoundError("404"), LgtvError),
        (WebOsTvResponseTypeError({"error": "401 insufficient permissions"}), PermissionDenied),
        (WebOsTvResponseTypeError({"error": "500"}), LgtvError),
        (WebOsTvCommandTimeoutError("slow"), Unreachable),
        (WebOsTvCommandError("Not connected, can't execute command."), ConnectionLost),
        (WebOsTvCommandError("Request failed with response {'returnValue': False}"), Rejected),
    ],
)
async def test_library_errors_are_mapped(
    network: Network,
    paired: TvEntry,
    cfg_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: type[Exception],
) -> None:
    ctrl = await connected(paired, cfg_path)

    async def boom(*_: object) -> None:
        raise error

    monkeypatch.setattr(last_client(), "launch_app", boom)
    with pytest.raises(expected) as info:
        await ctrl.launch("netflix")
    assert "401" not in str(info.value)


async def test_request_id_is_not_mistaken_for_a_401(
    network: Network, paired: TvEntry, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctrl = await connected(paired, cfg_path)

    async def server_error(*_: object) -> None:  # built like aiowebostv builds it
        raise WebOsTvResponseTypeError(
            {"type": "error", "id": 1401, "error": "500 Application error", "payload": {}}
        )

    monkeypatch.setattr(last_client(), "launch_app", server_error)
    with pytest.raises(LgtvError, match="rejected") as info:
        await ctrl.launch("netflix")
    assert not isinstance(info.value, PermissionDenied)


async def test_refusal_mentioning_not_connected_is_not_retried(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    attempts = 0

    class Refusing(FakeClient):
        async def set_input(self, input_id: str) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            raise WebOsTvCommandError(
                "Request failed with response {'payload': {'returnValue': False, "
                "'errorText': 'Device is not connected'}}"
            )

    pool = ControllerPool(cfg_path, client_factory=Refusing)
    with pytest.raises(Rejected):
        await pool.run(None, lambda c: c.switch_input("ps5"))
    assert attempts == 1
    assert len(FakeClient.instances) == 1
    await pool.close()


class DroppedMidRequest(FakeClient):
    """The socket closes while a reply is pending: aiowebostv cancels the reply future."""

    async def launch_app(self, app_id: str) -> dict[str, Any]:
        self.calls.append(("launch_app", app_id))
        reply: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(reply.cancel)
        return await reply


async def test_connection_dropped_mid_request_is_an_error_not_a_cancellation(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    pool = ControllerPool(cfg_path, client_factory=DroppedMidRequest)
    with pytest.raises(Unreachable, match="before it answered"):
        await pool.run(None, lambda c: c.launch("netflix"))
    # Not retried: the launch may already have reached the TV.
    assert [c for c in last_client().calls if c[0] == "launch_app"] == [("launch_app", "netflix")]
    await pool.close()


async def test_timeout_during_a_request_still_times_out(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    class NoReply(FakeClient):
        async def launch_app(self, app_id: str) -> dict[str, Any]:
            await asyncio.sleep(10)
            return {}

    pool = ControllerPool(cfg_path, client_factory=NoReply)
    with pytest.raises(Unreachable, match="did not respond in time"):
        await pool.run(None, lambda c: c.launch("netflix"), timeout=0.05)
    await pool.close()


# --- pairing ----------------------------------------------------------------------


async def test_pair_returns_entry_with_identity(network: Network) -> None:
    entry = await pair("192.168.4.40", client_factory=FakeClient)
    assert entry.key == "new-key"
    assert entry.uuid == TV_UUID
    assert entry.model == "50UM7360"
    assert entry.macs == ["02:ab:cd:00:00:01", "02:ab:cd:00:00:02"]
    assert not last_client().connected


async def test_pair_cancelled(network: Network) -> None:
    FakeClient.pair_key = None
    with pytest.raises(PairingFailed, match="cancelled or timed out"):
        await pair("192.168.4.40", client_factory=FakeClient)


async def test_pair_rejects_public_host(network: Network) -> None:
    with pytest.raises(InvalidInput):
        await pair("8.8.8.8", client_factory=FakeClient)


async def test_pair_unreachable(network: Network) -> None:
    with pytest.raises(Unreachable):
        await pair("192.168.4.99", client_factory=FakeClient)


async def test_pair_maps_a_closed_handshake(network: Network) -> None:
    FakeClient.connect_error = HANDSHAKE_CLOSED
    with pytest.raises(Unreachable, match="while pairing"):
        await pair("192.168.4.40", client_factory=FakeClient)


async def test_pairing_window_restores_timeout(network: Network) -> None:
    from aiowebostv import webos_client

    before = webos_client.RECEIVE_TIMEOUT
    seen: list[float] = []

    class SlowClient(FakeClient):
        async def connect(self) -> bool:
            seen.append(webos_client.RECEIVE_TIMEOUT)
            return await super().connect()

    await pair("192.168.4.40", client_factory=SlowClient)
    assert seen == [60.0]
    assert before == webos_client.RECEIVE_TIMEOUT


# --- pool -------------------------------------------------------------------------


async def test_pool_reuses_connection(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    await pool.run(None, lambda c: c.launch("netflix"))
    await pool.run("living", lambda c: c.launch("youtube"))
    assert len(FakeClient.instances) == 1
    await pool.close()
    assert not last_client().connected


async def test_pool_reconnects_once_after_drop(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    await pool.run(None, lambda c: c.launch("netflix"))
    last_client().connected = False
    await pool.run(None, lambda c: c.launch("youtube"))
    assert len(FakeClient.instances) == 2
    await pool.close()


async def test_pool_gives_up_after_second_drop(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    calls = 0

    async def always_drops(_: TvController) -> None:
        nonlocal calls
        calls += 1
        raise ConnectionLost("gone")

    with pytest.raises(ConnectionLost):
        await pool.run(None, always_drops)
    assert calls == 2
    await pool.close()


async def test_pool_timeout(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)

    async def hang(_: TvController) -> None:
        await asyncio.sleep(10)

    with pytest.raises(Unreachable, match="did not respond in time"):
        await pool.run(None, hang, timeout=0.05)
    await pool.close()


async def test_pool_picks_up_repairing(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    await pool.run(None, lambda c: c.launch("netflix"))
    config.update(lambda c: c.add("living", TvEntry(host="192.168.4.40", key="rotated")), cfg_path)
    await pool.run(None, lambda c: c.launch("netflix"))
    assert last_client().client_key == "rotated"
    assert not FakeClient.instances[0].connected
    await pool.close()


async def test_pool_does_not_close_a_controller_in_use(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    release = asyncio.Event()
    in_flight = asyncio.Event()

    class Slow(FakeClient):
        async def launch_app(self, app_id: str) -> dict[str, Any]:
            in_flight.set()
            await release.wait()
            if not self.connected:  # the real client cancels pending replies on disconnect
                raise asyncio.CancelledError
            return await super().launch_app(app_id)

    pool = ControllerPool(cfg_path, client_factory=Slow)
    first = asyncio.create_task(pool.run(None, lambda c: c.launch("netflix")))
    await asyncio.wait_for(in_flight.wait(), 5)
    config.update(lambda c: c.add("living", replace(paired, key="rotated")), cfg_path)
    second = asyncio.create_task(pool.run(None, lambda c: c.launch("youtube")))
    await asyncio.sleep(0.01)
    release.set()
    assert await first == "Netflix"
    assert await second == "YouTube"
    assert last_client().client_key == "rotated"
    await pool.close()
    assert not any(client.connected for client in FakeClient.instances)


async def test_pool_closes_connections_to_removed_tvs(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    network.reachable_hosts.add("192.168.4.41")
    config.update(lambda c: c.add("bedroom", TvEntry(host="192.168.4.41", key="k2")), cfg_path)
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    await pool.run("living", lambda c: c.launch("netflix"))
    living = last_client()
    config.update(lambda c: c.remove("living"), cfg_path)
    await pool.run("bedroom", lambda c: c.launch("netflix"))
    assert not living.connected
    await pool.close()


async def wait_for_job(pool: ControllerPool, name: str) -> str:
    for _ in range(100):
        job = pool.pairing_status(name)
        if job.state != "waiting":
            return job.state
        await asyncio.sleep(0.01)
    raise AssertionError("pairing never finished")


async def test_pool_background_pairing(network: Network, cfg_path: Path) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    job = await pool.start_pairing("192.168.4.40", "bedroom", make_default=True)
    assert job.state == "waiting"
    assert await wait_for_job(pool, "bedroom") == "paired"
    cfg = config.load(cfg_path)
    assert cfg.default == "bedroom"
    assert cfg.tvs["bedroom"].key == "new-key"
    await pool.close()


async def test_pool_background_pairing_failure(network: Network, cfg_path: Path) -> None:
    FakeClient.pair_key = None
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    await pool.start_pairing("192.168.4.40", "bedroom", make_default=False)
    assert await wait_for_job(pool, "bedroom") == "failed"
    assert "cancelled" in pool.pairing_status("bedroom").message
    assert config.load(cfg_path).tvs == {}
    await pool.close()


async def test_pool_pairing_validates_immediately(network: Network, cfg_path: Path) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    with pytest.raises(InvalidInput):
        await pool.start_pairing("192.168.4.40", "Bad Name", make_default=False)
    with pytest.raises(InvalidInput):
        await pool.start_pairing("8.8.8.8", "ok", make_default=False)
    with pytest.raises(Unreachable):
        await pool.start_pairing("192.168.4.99", "ok", make_default=False)
    with pytest.raises(NotFound):
        pool.pairing_status("never")
    await pool.close()


async def test_refusal_is_not_retried(
    network: Network, paired: TvEntry, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    attempts = 0

    async def refuse(_: TvController) -> None:
        nonlocal attempts
        attempts += 1
        raise Rejected()

    with pytest.raises(Rejected):
        await pool.run(None, refuse)
    assert attempts == 1
    assert len(FakeClient.instances) == 1
    await pool.close()


async def test_pool_without_retry_runs_once(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    attempts = 0

    async def drops(_: TvController) -> None:
        nonlocal attempts
        attempts += 1
        raise ConnectionLost("gone")

    with pytest.raises(ConnectionLost):
        await pool.run(None, drops, retry=False)
    assert attempts == 1
    await pool.close()


async def test_connect_cleans_up_on_cancellation(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    started = asyncio.Event()

    class HangingClient(FakeClient):
        async def connect(self) -> bool:
            started.set()
            await asyncio.sleep(10)
            return True

    ctrl = TvController("living", paired, config_path=cfg_path, client_factory=HangingClient)
    task = asyncio.create_task(ctrl.connect())
    # Cancel only once the handshake is underway; host checks run in threads first.
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("disconnect", None) in last_client().calls
    assert not ctrl.is_connected


async def test_overlapping_pairings_restore_timeout(network: Network) -> None:
    from aiowebostv import webos_client

    before = webos_client.RECEIVE_TIMEOUT
    release = asyncio.Event()
    both_waiting = asyncio.Event()
    seen: list[float] = []

    class GatedClient(FakeClient):
        async def connect(self) -> bool:
            seen.append(webos_client.RECEIVE_TIMEOUT)
            if len(seen) == 2:
                both_waiting.set()
            await release.wait()
            return await super().connect()

    first = asyncio.create_task(pair("192.168.4.40", client_factory=GatedClient))
    second = asyncio.create_task(pair("192.168.4.40", client_factory=GatedClient))
    # Both pairings must be inside the window at once for this to test overlap.
    await asyncio.wait_for(both_waiting.wait(), 5)
    release.set()
    await asyncio.gather(first, second)
    assert seen == [60, 60]
    assert before == webos_client.RECEIVE_TIMEOUT


async def test_pairing_cannot_start_twice(network: Network, cfg_path: Path) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    results = await asyncio.gather(
        pool.start_pairing("192.168.4.40", "bedroom", make_default=False),
        pool.start_pairing("192.168.4.40", "bedroom", make_default=False),
        return_exceptions=True,
    )
    assert sum(isinstance(r, InvalidInput) for r in results) == 1
    await pool.close()


async def test_pairing_refuses_to_overwrite(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    with pytest.raises(InvalidInput, match="already paired"):
        await pool.start_pairing("192.168.4.40", "living", make_default=False)
    await pool.start_pairing("192.168.4.40", "living", make_default=False, replace=True)
    assert await wait_for_job(pool, "living") == "paired"
    assert config.load(cfg_path).tvs["living"].key == "new-key"
    await pool.close()


async def test_move_updates_host_and_reconnects(
    network: Network, paired: TvEntry, cfg_path: Path
) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    await pool.run(None, lambda c: c.launch("netflix"))
    network.reachable_hosts = {"192.168.4.77"}
    assert await pool.move("living", "192.168.4.77") == "192.168.4.77"
    assert config.load(cfg_path).tvs["living"].host == "192.168.4.77"
    await pool.run(None, lambda c: c.launch("netflix"))
    assert last_client().host == "192.168.4.77"
    await pool.close()


@pytest.mark.parametrize(
    ("name", "host", "error"),
    [
        ("living", "8.8.8.8", InvalidInput),
        ("living", "192.168.4.99", Unreachable),
        ("kitchen", "192.168.4.40", LgtvError),
    ],
)
async def test_move_validates(
    network: Network,
    paired: TvEntry,
    cfg_path: Path,
    name: str,
    host: str,
    error: type[Exception],
) -> None:
    pool = ControllerPool(cfg_path, client_factory=FakeClient)
    with pytest.raises(error):
        await pool.move(name, host)
    assert config.load(cfg_path).tvs["living"].host == "192.168.4.40"


async def test_status_coerces_tv_values(network: Network, paired: TvEntry, cfg_path: Path) -> None:
    FakeClient.state_overrides = {"volume": "loud\x1b", "muted": 1, "current_app_id": "x\x1b\ny"}
    status = (await connected(paired, cfg_path)).status()
    assert status["volume"] is None
    assert status["muted"] is True
    assert status["app_id"] == "x y"
