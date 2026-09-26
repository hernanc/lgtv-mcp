import json
from pathlib import Path

import pytest

from lgtv_mcp import cli, config
from lgtv_mcp.config import TvEntry
from lgtv_mcp.control import ControllerPool
from lgtv_mcp.discovery import Found

from .conftest import Network
from .fakes import TV_UUID, FakeClient


@pytest.fixture(autouse=True)
def fake_pool(monkeypatch: pytest.MonkeyPatch, network: Network, cfg_path: Path) -> None:
    monkeypatch.setattr(cli, "ControllerPool", lambda path: ControllerPool(path, FakeClient))


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    cli.main(list(argv))
    return capsys.readouterr().out


def last_calls() -> list[tuple[str, object]]:
    return FakeClient.instances[-1].calls


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["yt", "-8DNbZkeRTs"], ["yt", "--", "-8DNbZkeRTs"]),
        (["--tv", "living", "yt", "-8DNbZkeRTs"], ["--tv", "living", "yt", "--", "-8DNbZkeRTs"]),
        (["--json", "yt", "-8DNbZkeRTs"], ["--json", "yt", "--", "-8DNbZkeRTs"]),
        (["yt", "dQw4w9WgXcQ"], ["yt", "dQw4w9WgXcQ"]),
        (["yt", "--help"], ["yt", "--help"]),
        (["msg", "yt", "-x"], ["msg", "yt", "-x"]),
        (["--tv", "yt", "status"], ["--tv", "yt", "status"]),
        ([], []),
    ],
)
def test_normalize_argv(argv: list[str], expected: list[str]) -> None:
    assert cli._normalize_argv(argv) == expected


def test_status(capsys: pytest.CaptureFixture[str], paired: TvEntry) -> None:
    out = run(capsys, "status")
    assert "Showing: Live TV" in out
    assert "Channel: 81-6 La Nacion HD  (Noticias)" in out


def test_status_json(capsys: pytest.CaptureFixture[str], paired: TvEntry) -> None:
    data = json.loads(run(capsys, "--json", "status"))
    assert data["volume"] == 12


def test_msg(capsys: pytest.CaptureFixture[str], paired: TvEntry) -> None:
    run(capsys, "msg", "Hola", "Mundo")
    assert (
        "request",
        ("system.notifications/createToast", {"message": "Hola Mundo"}),
    ) in last_calls()


def test_yt_with_dash_id(capsys: pytest.CaptureFixture[str], paired: TvEntry) -> None:
    run(capsys, "yt", "-8DNbZkeRTs")
    target = {"contentTarget": "https://www.youtube.com/tv?v=-8DNbZkeRTs"}
    assert ("launch_app_with_params", ("youtube.leanback.v4", target)) in last_calls()


def test_app_and_input(capsys: pytest.CaptureFixture[str], paired: TvEntry) -> None:
    assert "Opened Prime Video." in run(capsys, "app", "prime", "video")
    assert "Switched to PS5." in run(capsys, "input", "ps5")


@pytest.mark.parametrize(
    ("argv", "call"),
    [
        (["vol", "15"], ("set_volume", 15)),
        (["vol", "up"], ("volume_up", None)),
        (["mute"], ("set_mute", True)),
        (["unmute"], ("set_mute", False)),
        (["screen", "off"], ("set_screen_state", False)),
        (["off"], ("power_off", None)),
        (["key", "home"], ("button", "HOME")),
    ],
)
def test_simple_commands(
    capsys: pytest.CaptureFixture[str], paired: TvEntry, argv: list[str], call: tuple[str, object]
) -> None:
    run(capsys, *argv)
    assert call in last_calls()


def test_vol_show(capsys: pytest.CaptureFixture[str], paired: TvEntry) -> None:
    assert run(capsys, "vol").strip() == "12"


@pytest.mark.parametrize("level", ["loud", "-5", "1e3", "101"])
def test_vol_rejects_bad_values(
    capsys: pytest.CaptureFixture[str], paired: TvEntry, level: str
) -> None:
    with pytest.raises(SystemExit) as err:
        cli.main(["vol", "--", level])
    assert "lgtv:" in str(err.value.code)


def test_errors_exit_with_message(paired: TvEntry) -> None:
    with pytest.raises(SystemExit) as err:
        cli.main(["app", "play"])
    assert str(err.value.code).startswith("lgtv: 'play' matches several apps")


def test_no_tv_paired() -> None:
    with pytest.raises(SystemExit) as err:
        cli.main(["status"])
    assert "No TV paired yet" in str(err.value.code)


def test_pair(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, cfg_path: Path
) -> None:
    async def fake_pair(host: str) -> TvEntry:
        return TvEntry(host=host, key="k", uuid=TV_UUID, model="50UM7360")

    monkeypatch.setattr(cli, "pair", fake_pair)
    out = run(capsys, "pair", "192.168.4.40", "--name", "living")
    assert "Paired 'living': 50UM7360 at 192.168.4.40 (default)." in out
    assert config.load(cfg_path).tvs["living"].key == "k"


def test_pair_rejects_bad_name_before_prompting(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_pair(host: str) -> TvEntry:
        raise AssertionError("should not pair")

    monkeypatch.setattr(cli, "pair", fake_pair)
    with pytest.raises(SystemExit):
        cli.main(["pair", "192.168.4.40", "--name", "Bad Name"])


def test_list_default_remove(
    capsys: pytest.CaptureFixture[str], paired: TvEntry, cfg_path: Path
) -> None:
    config.update(lambda c: c.add("bedroom", TvEntry(host="192.168.4.41", key="k2")), cfg_path)
    out = run(capsys, "list")
    assert "* living" in out
    assert "saved-key" not in out
    run(capsys, "default", "bedroom")
    assert config.load(cfg_path).default == "bedroom"
    run(capsys, "remove", "bedroom")
    assert config.load(cfg_path).default == "living"
    with pytest.raises(SystemExit):
        cli.main(["default", "nope"])


def test_list_json_has_no_keys(capsys: pytest.CaptureFixture[str], paired: TvEntry) -> None:
    out = run(capsys, "--json", "list")
    assert "saved-key" not in out
    assert json.loads(out)[0]["name"] == "living"


def test_discover(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, paired: TvEntry
) -> None:
    monkeypatch.setattr(cli, "discover", lambda: [Found("192.168.4.40", TV_UUID, "WebOS/4.1.0")])
    assert "(paired as 'living')" in run(capsys, "discover")


def test_select_tv(
    capsys: pytest.CaptureFixture[str], paired: TvEntry, cfg_path: Path, network: Network
) -> None:
    config.update(lambda c: c.add("bedroom", TvEntry(host="192.168.4.41", key="k2")), cfg_path)
    network.reachable_hosts.add("192.168.4.41")
    run(capsys, "--tv", "bedroom", "mute")
    assert FakeClient.instances[-1].host == "192.168.4.41"


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.startswith("lgtv ")


def test_actions_print_messages(capsys: pytest.CaptureFixture[str], paired: TvEntry) -> None:
    assert run(capsys, "mute").strip() == "Muted."
    assert run(capsys, "key", "home", "enter").strip() == "Pressed home enter."


@pytest.mark.parametrize(
    "argv",
    [["mute"], ["off"], ["yt", "dQw4w9WgXcQ"], ["vol", "up"], ["msg", "hi"], ["app", "netflix"]],
)
def test_json_output_for_actions(
    capsys: pytest.CaptureFixture[str], paired: TvEntry, argv: list[str]
) -> None:
    data = json.loads(run(capsys, "--json", *argv))
    assert "message" in data


def test_abbreviated_options_are_rejected(paired: TvEntry) -> None:
    with pytest.raises(SystemExit) as err:
        cli.main(["--t", "living", "status"])
    assert err.value.code == 2


def test_move(
    capsys: pytest.CaptureFixture[str], paired: TvEntry, cfg_path: Path, network: Network
) -> None:
    network.reachable_hosts.add("192.168.4.77")
    assert "now uses 192.168.4.77" in run(capsys, "move", "living", "192.168.4.77")
    assert config.load(cfg_path).tvs["living"].host == "192.168.4.77"


def test_pair_requires_replace_for_existing_name(
    monkeypatch: pytest.MonkeyPatch,
    paired: TvEntry,
    capsys: pytest.CaptureFixture[str],
    cfg_path: Path,
) -> None:
    async def fake_pair(host: str) -> TvEntry:
        return TvEntry(host=host, key="rotated")

    monkeypatch.setattr(cli, "pair", fake_pair)
    with pytest.raises(SystemExit) as err:
        cli.main(["pair", "192.168.4.40", "--name", "living"])
    assert "already paired" in str(err.value.code)
    run(capsys, "pair", "192.168.4.40", "--name", "living", "--replace")
    assert config.load(cfg_path).tvs["living"].key == "rotated"
