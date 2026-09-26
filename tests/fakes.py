"""A stand-in for aiowebostv.WebOsClient that records calls instead of talking to a TV."""

from types import SimpleNamespace
from typing import Any, ClassVar

from aiowebostv.exceptions import WebOsTvPairError

TV_UUID = "3f2b8c1e-5d4a-4e6b-9c7d-0a1b2c3d4e5f"


def make_state(**overrides: Any) -> SimpleNamespace:
    state = SimpleNamespace(
        power_state={"state": "Active"},
        is_on=True,
        is_screen_on=True,
        current_app_id="com.webos.app.livetv",
        volume=12,
        muted=False,
        sound_output="external_optical",
        apps={
            "netflix": {"title": "Netflix"},
            "youtube.leanback.v4": {"title": "YouTube"},
            "amazon": {"title": "Prime Video"},
            "com.playworks.app.tetris": {"title": "Tetris"},
            "googleplaymovieswebos": {"title": "Google Play Movies & TV"},
        },
        inputs={
            "com.webos.app.hdmi1": {
                "id": "HDMI_1",
                "label": "HDMI 1",
                "appId": "com.webos.app.hdmi1",
            },
            "com.webos.app.hdmi2": {
                "id": "HDMI_2",
                "label": "PS5",
                "appId": "com.webos.app.hdmi2",
                "connected": True,
            },
        },
        current_channel={"channelNumber": "81-6", "channelName": "La Nacion HD"},
        channel_info={"programList": [{"programName": "Noticias"}]},
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class FakeClient:
    """Records every call in ``calls``. Class attributes steer behavior per test."""

    instances: ClassVar[list["FakeClient"]] = []
    connect_error: ClassVar[BaseException | None] = None
    pair_key: ClassVar[str | None] = "new-key"
    state_overrides: ClassVar[dict[str, Any]] = {}

    def __init__(self, host: str, client_key: str | None = None) -> None:
        self.host = host
        self.client_key = client_key
        self.connected = False
        self.calls: list[tuple[str, Any]] = []
        self.tv_state = make_state(**FakeClient.state_overrides)
        self.tv_info = SimpleNamespace(
            hello={"deviceUUID": TV_UUID, "deviceOSReleaseVersion": "4.10.2"},
            system={"modelName": "50UM7360"},
            connection={
                "wifiInfo": {"macAddress": "02:AB:CD:00:00:01"},
                "wiredInfo": {"macAddress": "02:AB:CD:00:00:02"},
            },
        )
        FakeClient.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.connect_error = None
        cls.pair_key = "new-key"
        cls.state_overrides = {}

    async def connect(self) -> bool:
        if FakeClient.connect_error is not None:
            raise FakeClient.connect_error
        if self.client_key is None:
            if FakeClient.pair_key is None:
                raise WebOsTvPairError("403 cancelled")
            self.client_key = FakeClient.pair_key
        self.connected = True
        return True

    async def disconnect(self) -> None:
        self.connected = False
        self.calls.append(("disconnect", None))

    def is_connected(self) -> bool:
        return self.connected

    async def _record(self, name: str, arg: Any = None) -> dict[str, Any]:
        self.calls.append((name, arg))
        return {"returnValue": True}

    async def launch_app(self, app_id: str) -> dict[str, Any]:
        return await self._record("launch_app", app_id)

    async def launch_app_with_params(self, app_id: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._record("launch_app_with_params", (app_id, params))

    async def set_input(self, input_id: str) -> dict[str, Any]:
        return await self._record("set_input", input_id)

    async def set_volume(self, level: int) -> dict[str, Any]:
        return await self._record("set_volume", level)

    async def volume_up(self) -> dict[str, Any]:
        return await self._record("volume_up")

    async def volume_down(self) -> dict[str, Any]:
        return await self._record("volume_down")

    async def set_mute(self, muted: bool) -> dict[str, Any]:
        return await self._record("set_mute", muted)

    async def set_screen_state(self, on: bool) -> None:
        await self._record("set_screen_state", on)

    async def power_off(self) -> None:
        await self._record("power_off")

    async def power_on(self) -> dict[str, Any]:
        return await self._record("power_on")

    async def button(self, name: str) -> None:
        await self._record("button", name)

    async def request(self, uri: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._record("request", (uri, payload))
