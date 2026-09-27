"""Async TV control over aiowebostv, shared by the CLI and the MCP server."""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, TypeVar

import aiohttp
from aiowebostv import WebOsClient
from aiowebostv import webos_client as _webos_client
from aiowebostv.buttons import BUTTONS
from aiowebostv.exceptions import (
    WebOsTvCommandError,
    WebOsTvCommandTimeoutError,
    WebOsTvError,
    WebOsTvPairError,
    WebOsTvResponseTypeError,
    WebOsTvServiceNotFoundError,
)

from . import config
from .config import TvEntry
from .discovery import (
    discover,
    local_ipv4,
    normalize_mac,
    reachable,
    validate_host_async,
    wake_on_lan,
)
from .errors import (
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
from .matching import clean_text, pick
from .youtube import YOUTUBE_APP_ID, youtube_target

log = logging.getLogger(__name__)

T = TypeVar("T")
ClientFactory = Callable[[str, str | None], Any]

LIVE_TV_APP_ID = "com.webos.app.livetv"
SYSTEM_APPS = {LIVE_TV_APP_ID: "Live TV"}
# POWER is left out so that turning the TV off always goes through the power
# tool, which clients can ask about before running.
KEYS = frozenset(BUTTONS) - {"POWER"}
CONNECT_TIMEOUT = 10.0
OPERATION_TIMEOUT = 20.0
PAIR_TIMEOUT = 60.0
MAX_TOAST = 200
MAX_KEYS = 20
_KEY_GAP = 0.15
_TOAST_URI = "system.notifications/createToast"

# What client.connect() raises when the TV cannot be reached or breaks off the
# handshake. aiohttp raises WSMessageTypeError, a TypeError, when the socket
# closes mid-handshake, and aiowebostv indexes the TV's replies directly.
_CONNECT_ERRORS = (OSError, ValueError, TypeError, KeyError, aiohttp.ClientError, WebOsTvError)


def _default_client(host: str, client_key: str | None) -> Any:
    """A WebOsClient whose HTTP session only reaches local IPv4 hosts.

    The TV answers the WebSocket upgrade and names the URL of its input socket
    itself, so without this a device at an allowed address could redirect the
    connection, including the registration that carries the key, anywhere.
    """
    trace = aiohttp.TraceConfig()
    trace.on_request_start.append(_refuse_outside_hosts)
    trace.on_request_redirect.append(_refuse_redirects)
    session = aiohttp.ClientSession(trace_configs=[trace])
    return WebOsClient(host, client_key, client_session=session)


async def _refuse_outside_hosts(
    _session: aiohttp.ClientSession,
    _ctx: SimpleNamespace,
    params: aiohttp.TraceRequestStartParams,
) -> None:
    if local_ipv4(params.url.host or "") is None:
        raise aiohttp.ClientConnectionError(
            f"Refusing to connect to {params.url.host}: not on the local network."
        )


async def _refuse_redirects(
    _session: aiohttp.ClientSession,
    _ctx: SimpleNamespace,
    params: aiohttp.TraceRequestRedirectParams,
) -> None:
    params.response.close()
    raise aiohttp.ClientConnectionError("The TV tried to redirect the connection.")


class TvController:
    """One TV. Connects lazily; every method raises LgtvError on expected failures."""

    def __init__(
        self,
        name: str,
        entry: TvEntry,
        *,
        config_path: Path | None = None,
        client_factory: ClientFactory = _default_client,
    ) -> None:
        self.name = name
        self.entry = entry
        self._config_path = config_path
        self._factory = client_factory
        self._client: Any = None

    # --- connection ---------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._client is not None and bool(self._client.is_connected())

    async def connect(self) -> None:
        if self.is_connected:
            return
        await self.close()
        host = await self._resolve_host()
        client = self._factory(host, self.entry.key)
        try:
            await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)
        except WebOsTvPairError as err:
            await _disconnect(client)
            raise PairingFailed(
                f"TV '{self.name}' rejected the saved pairing key. Pair it again."
            ) from err
        except TimeoutError as err:
            await _disconnect(client)
            raise Unreachable(
                f"TV '{self.name}' at {host} did not finish connecting. If it shows a "
                "connection prompt, accept it with the remote and try again."
            ) from err
        except _CONNECT_ERRORS as err:
            await _disconnect(client)
            raise Unreachable(f"Could not connect to TV '{self.name}' at {host}.") from err
        except BaseException:  # cancellation or a bug: never leave the socket open
            await _disconnect(client)
            raise
        identity = _identity(client)
        if self.entry.uuid and identity["uuid"] and identity["uuid"] != self.entry.uuid:
            await _disconnect(client)
            raise Unreachable(
                f"The device at {host} is not TV '{self.name}' (it reports a different id). "
                "If the TV's address changed, confirm the new one with lgtv move or "
                "set_tv_address."
            )
        self._client = client
        self._record(identity)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await _disconnect(client)

    async def _resolve_host(self) -> str:
        host = await validate_host_async(self.entry.host)
        if await asyncio.to_thread(reachable, host):
            return host
        if self.entry.uuid:
            try:
                found = await asyncio.to_thread(discover)
            except LgtvError:  # the search itself failed; report the TV as unreachable
                found = []
            for tv in found:
                if tv.uuid == self.entry.uuid and tv.host != host:
                    raise TvMoved(self.name, host, tv.host)
        raise Unreachable(
            f"TV '{self.name}' is not reachable (last seen at {host}). "
            "It may be off or on another network."
        )

    def _record(self, identity: dict[str, Any]) -> None:
        """Save a key the TV issued on this connect (it prompts again when it no
        longer knows the saved one) and identity that older entries lack.

        Only those fields are merged into the saved entry, so a host changed
        meanwhile (lgtv move, set_tv_address) is kept.
        """
        old = self.entry
        new = replace(
            old,
            key=self._client.client_key or old.key,
            uuid=old.uuid or identity["uuid"],
            model=old.model or identity["model"],
            macs=old.macs or identity["macs"],
        )
        if new == old:
            return

        def apply(cfg: config.Config) -> None:
            current = cfg.tvs.get(self.name)
            if current is not None and current.key == old.key:  # not re-paired meanwhile
                cfg.tvs[self.name] = replace(
                    current,
                    key=new.key,
                    uuid=current.uuid or new.uuid,
                    model=current.model or new.model,
                    macs=current.macs or new.macs,
                )

        try:
            config.update(apply, self._config_path)
        except (OSError, LgtvError) as err:
            log.warning("Could not update config for TV '%s': %s", self.name, err)
            new = replace(new, key=old.key)  # keep matching the saved key the pool compares
        self.entry = new

    # --- reads ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        state = self._state()
        app_id = clean_text(state.current_app_id) or None
        if state.is_on:
            power = "on" if state.is_screen_on else "screen off"
        else:
            power = "standby"
        result: dict[str, Any] = {
            "tv": self.name,
            "power": power,
            "app_id": app_id,
            "app": self._app_label(app_id),
            "volume": state.volume if isinstance(state.volume, int) else None,
            "muted": bool(state.muted) if state.muted is not None else None,
            "sound_output": clean_text(state.sound_output) or None,
        }
        if app_id == LIVE_TV_APP_ID and state.current_channel:
            channel = state.current_channel
            result["channel"] = clean_text(
                f"{channel.get('channelNumber', '')} {channel.get('channelName', '')}"
            )
            programs = (state.channel_info or {}).get("programList") or []
            if programs and isinstance(programs[0], dict):
                result["program"] = clean_text(programs[0].get("programName")) or None
        return result

    def info(self) -> dict[str, Any]:
        tv_info = self._connected().tv_info
        return {
            "tv": self.name,
            "model": clean_text(tv_info.system.get("modelName")) or self.entry.model,
            "webos_version": clean_text(tv_info.hello.get("deviceOSReleaseVersion")) or None,
            "host": self._client.host,
            "uuid": self.entry.uuid,
            "macs": self.entry.macs,
        }

    def apps(self) -> list[dict[str, str]]:
        titles = self._app_titles()
        return [{"id": i, "title": t} for i, t in sorted(titles.items(), key=lambda kv: kv[1])]

    def inputs(self) -> list[dict[str, Any]]:
        rows = [
            {
                "id": clean_text(raw.get("id")),
                "label": clean_text(raw.get("label") or raw.get("id")),
                "connected": bool(raw.get("connected", False)),
            }
            for raw in self._state().inputs.values()
            if isinstance(raw, dict) and raw.get("id")
        ]
        return sorted(rows, key=lambda r: r["label"])

    # --- controls ---------------------------------------------------------------

    async def launch(self, name: str) -> str:
        self._require_on()
        titles = self._app_titles()
        app_id = pick(name, titles, "app")
        await self._call(self._client.launch_app(app_id))
        return titles[app_id]

    async def switch_input(self, name: str) -> str:
        self._require_on()
        labels = {str(row["id"]): str(row["label"]) for row in self.inputs()}
        input_id = pick(name, labels, "input")
        await self._call(self._client.set_input(input_id))
        return labels[input_id]

    async def youtube(self, url_or_id: str) -> str:
        target = youtube_target(url_or_id)
        self._require_on()
        await self._call(
            self._client.launch_app_with_params(YOUTUBE_APP_ID, {"contentTarget": target})
        )
        return target

    async def set_volume(self, level: int) -> int:
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100:
            raise InvalidInput("Volume must be a whole number from 0 to 100.")
        self._require_on()
        await self._call(self._client.set_volume(level))
        return level

    async def step_volume(self, direction: Literal["up", "down"]) -> None:
        self._require_on()
        step = self._client.volume_up if direction == "up" else self._client.volume_down
        await self._call(step())

    async def set_mute(self, muted: bool) -> None:
        self._require_on()
        await self._call(self._client.set_mute(muted))

    async def set_screen(self, on: bool) -> None:
        self._require_on()
        await self._call(self._client.set_screen_state(on))

    async def power_off(self) -> bool:
        """Turn the TV off. Returns False if it was already off."""
        if not self._state().is_on:
            return False
        await self._call(self._client.power_off())
        return True

    async def power_on(self) -> str:
        """Wake the TV: over the control API if it is in network standby, else Wake-on-LAN."""
        host = await validate_host_async(self.entry.host)
        if await asyncio.to_thread(reachable, host):
            with suppress(LgtvError):
                await self.connect()
            if self.is_connected:
                if self._client.tv_state.is_on:
                    return "The TV is already on."
                with suppress(LgtvError):
                    await self._call(self._client.power_on())
                    return "Turned the TV on."
        await asyncio.to_thread(wake_on_lan, self.entry.macs, host)
        return (
            "Sent Wake-on-LAN. The TV should turn on within a few seconds if "
            "'Turn on via Wi-Fi' (or 'Mobile TV On') is enabled in its settings."
        )

    async def press(self, keys: list[str]) -> list[str]:
        if not keys:
            raise InvalidInput("Give at least one key.")
        if len(keys) > MAX_KEYS:
            raise InvalidInput(f"At most {MAX_KEYS} keys per call.")
        buttons = [k.strip().upper() for k in keys]
        unknown = [clean_text(k) for k, b in zip(keys, buttons, strict=True) if b not in KEYS]
        if unknown:
            known = ", ".join(sorted(k.lower() for k in KEYS))
            raise NotFound(f"Unknown key(s): {', '.join(unknown)}. Known keys: {known}.")
        client = self._connected()
        for i, button in enumerate(buttons):
            if i:
                await asyncio.sleep(_KEY_GAP)
            await self._call(client.button(button))
        return buttons

    async def toast(self, text: str) -> str:
        message = clean_text(text.strip()) if isinstance(text, str) else ""
        if not message:
            raise InvalidInput("The message is empty.")
        if len(text.strip()) > MAX_TOAST:
            raise InvalidInput(f"Messages are limited to {MAX_TOAST} characters.")
        self._require_on()
        await self._call(self._client.request(_TOAST_URI, {"message": message}))
        return message

    # --- helpers ------------------------------------------------------------------

    def _connected(self) -> Any:
        if not self.is_connected:
            raise ConnectionLost(f"Not connected to TV '{self.name}'.")
        return self._client

    def _state(self) -> Any:
        return self._connected().tv_state

    def _require_on(self) -> None:
        if not self._state().is_on:
            raise TvOff(self.name)

    def _app_titles(self) -> dict[str, str]:
        titles = {
            clean_text(app_id): clean_text(app.get("title")) or clean_text(app_id)
            for app_id, app in self._state().apps.items()
            if isinstance(app, dict)
        }
        return {**SYSTEM_APPS, **titles}

    def _app_label(self, app_id: str | None) -> str | None:
        if not app_id:
            return None
        for raw in self._state().inputs.values():
            if isinstance(raw, dict) and raw.get("appId") == app_id:
                return clean_text(raw.get("label")) or None
        return self._app_titles().get(app_id) or clean_text(app_id)

    async def _call(self, awaitable: Awaitable[T]) -> T:
        try:
            return await awaitable
        except asyncio.CancelledError as err:
            # aiowebostv cancels the pending reply when the TV's socket closes. Only a
            # cancellation of this task itself (a timeout, shutdown) may propagate: a
            # stray one would end the MCP server's whole session. The request may
            # have been delivered, so this is not retried.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            raise Unreachable(
                f"Lost the connection to TV '{self.name}' before it answered. "
                "The action may or may not have happened."
            ) from err
        except WebOsTvServiceNotFoundError as err:
            raise LgtvError("This TV does not support that action.") from err
        except WebOsTvResponseTypeError as err:
            if _error_text(err).startswith("401"):
                raise PermissionDenied() from err
            raise LgtvError("The TV rejected the request.") from err
        except WebOsTvCommandTimeoutError as err:
            raise Unreachable(f"TV '{self.name}' did not respond in time.") from err
        except WebOsTvCommandError as err:
            # aiowebostv raises this both for "Not connected, ..." before sending and
            # for requests the TV refused (returnValue false); only the former is worth
            # a reconnect. A refusal's text comes from the TV, so match the prefix.
            if str(err).startswith("Not connected"):
                raise ConnectionLost(f"Lost the connection to TV '{self.name}'.") from err
            raise Rejected() from err
        except (aiohttp.ClientError, ConnectionError) as err:
            raise ConnectionLost(f"Lost the connection to TV '{self.name}'.") from err


def _error_text(err: WebOsTvResponseTypeError) -> str:
    """The TV's error string, such as '401 insufficient permissions'.

    str(err) is the whole reply, request id and TV-provided text included.
    """
    reply = err.args[0] if err.args else None
    return str(reply.get("error", "")) if isinstance(reply, dict) else str(reply or "")


async def pair(
    host: str,
    *,
    client_factory: ClientFactory = _default_client,
    timeout: float = PAIR_TIMEOUT,
) -> TvEntry:
    """Pair with the TV at ``host``. Someone must accept the prompt on the TV."""
    host = await validate_host_async(host)
    if not await asyncio.to_thread(reachable, host):
        raise Unreachable(f"No LG TV answered at {host}. Is it on and on this network?")
    client = client_factory(host, None)
    try:
        with _pairing_window(timeout):
            await asyncio.wait_for(client.connect(), timeout + 5)
        if not client.client_key:
            raise PairingFailed("The TV did not return a pairing key.")
        return TvEntry(host=host, key=client.client_key, **_identity(client))
    except (WebOsTvPairError, TimeoutError) as err:
        raise PairingFailed(
            "Pairing was cancelled or timed out on the TV. "
            "Retry while someone is at the TV to accept the prompt."
        ) from err
    except _CONNECT_ERRORS as err:
        raise Unreachable(f"Lost the connection to {host} while pairing.") from err
    finally:
        await _disconnect(client)


_pairing_depth = 0
_saved_receive_timeout: int | None = None


@contextmanager
def _pairing_window(seconds: float) -> Iterator[None]:
    """Give the user longer than aiowebostv's 10 s to accept the pairing prompt.

    aiowebostv reads the prompt answer with its module-wide RECEIVE_TIMEOUT.
    It is raised while any pairing is in progress and restored when the last
    one ends, so overlapping pairings cannot leave it changed. All callers run
    on one event loop thread, so the counter needs no lock.
    """
    global _pairing_depth, _saved_receive_timeout  # noqa: PLW0603
    current = getattr(_webos_client, "RECEIVE_TIMEOUT", None)
    if current is None:
        yield
        return
    if _pairing_depth == 0:
        _saved_receive_timeout = current
    _pairing_depth += 1
    _webos_client.RECEIVE_TIMEOUT = max(current, math.ceil(seconds))
    try:
        yield
    finally:
        _pairing_depth -= 1
        if _pairing_depth == 0 and _saved_receive_timeout is not None:
            _webos_client.RECEIVE_TIMEOUT = _saved_receive_timeout
            _saved_receive_timeout = None


def _identity(client: Any) -> dict[str, Any]:
    tv_info = client.tv_info
    macs: list[str] = []
    for kind in ("wifiInfo", "wiredInfo"):
        raw = (tv_info.connection.get(kind) or {}).get("macAddress")
        if isinstance(raw, str):
            with suppress(InvalidInput):
                if (mac := normalize_mac(raw)) not in macs:
                    macs.append(mac)
    return {
        "uuid": clean_text(tv_info.hello.get("deviceUUID")) or None,
        "model": clean_text(tv_info.system.get("modelName")) or None,
        "macs": macs,
    }


async def _disconnect(client: Any) -> None:
    with suppress(Exception):
        await client.disconnect()
    # aiowebostv only closes sessions it created itself; _default_client passes one in.
    session = getattr(client, "client_session", None)
    if isinstance(session, aiohttp.ClientSession) and not session.closed:
        with suppress(Exception):
            await session.close()


def ensure_can_add(cfg: config.Config, name: str, *, replace: bool) -> None:
    if name in cfg.tvs and not replace:
        raise InvalidInput(
            f"A TV named '{name}' is already paired. Choose another name, or replace it."
        )


def save_paired(
    name: str,
    entry: TvEntry,
    *,
    make_default: bool,
    replace: bool,
    path: Path | None = None,
) -> config.Config:
    def apply(cfg: config.Config) -> None:
        ensure_can_add(cfg, name, replace=replace)
        cfg.add(name, entry, make_default=make_default)

    return config.update(apply, path)


async def move_tv(name: str, host: str, path: Path | None = None) -> str:
    """Change a paired TV's address. Only call this on a person's confirmation."""
    new_host = await validate_host_async(host)
    config.load(path).get(name)
    if not await asyncio.to_thread(reachable, new_host):
        raise Unreachable(f"No LG TV answered at {new_host}.")

    def apply(cfg: config.Config) -> None:
        _, entry = cfg.get(name)
        cfg.tvs[name] = replace(entry, host=new_host)

    config.update(apply, path)
    return new_host


# --- pool used by the MCP server ------------------------------------------------


@dataclass
class PairingJob:
    host: str
    state: Literal["waiting", "paired", "failed"] = "waiting"
    message: str = "Accept the connection prompt on the TV with the remote."
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class ControllerPool:
    """Keeps one lazily connected controller per TV and serializes calls to it."""

    def __init__(
        self,
        config_path: Path | None = None,
        client_factory: ClientFactory = _default_client,
    ) -> None:
        self.config_path = config_path
        self._factory = client_factory
        self._controllers: dict[str, TvController] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pairings: dict[str, PairingJob] = {}

    def load_config(self) -> config.Config:
        return config.load(self.config_path)

    async def run(
        self,
        tv: str | None,
        operation: Callable[[TvController], Awaitable[T]],
        *,
        connect: bool = True,
        retry: bool = True,
        timeout: float = OPERATION_TIMEOUT,
    ) -> T:
        """Run ``operation`` on a TV.

        If the connection turns out to have dropped, reconnect and run it again
        once, unless ``retry`` is False (for operations that are not safe to
        repeat after a partial send, like key sequences or volume steps).
        """
        cfg = self.load_config()
        name, entry = cfg.get(tv)
        await self._forget_removed(cfg)
        attempts = 2 if retry else 1
        # The controller is looked up, replaced and closed only under its TV's lock,
        # so a call can never lose its connection to another call.
        async with self._lock(name):
            controller = await self._controller(name, entry)
            try:
                async with asyncio.timeout(timeout):
                    for attempt in range(1, attempts + 1):
                        try:
                            if connect:
                                await controller.connect()
                            return await operation(controller)
                        except ConnectionLost:
                            await controller.close()
                            if attempt == attempts:
                                raise
                    raise AssertionError("unreachable")  # pragma: no cover
            except TimeoutError as err:
                raise Unreachable(f"TV '{name}' did not respond in time.") from err

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    async def _controller(self, name: str, entry: TvEntry) -> TvController:
        """The pooled controller for a TV. Call with that TV's lock held."""
        controller = self._controllers.get(name)
        # Only the address and key matter to an open connection. The identity
        # fields can differ, for example when saving them failed.
        if controller is not None and (controller.entry.host, controller.entry.key) != (
            entry.host,
            entry.key,
        ):
            await controller.close()
            controller = None
        if controller is None:
            controller = TvController(
                name, entry, config_path=self.config_path, client_factory=self._factory
            )
            self._controllers[name] = controller
        return controller

    async def _forget_removed(self, cfg: config.Config) -> None:
        """Close idle connections to TVs that were removed from the config."""
        for name in list(self._controllers):
            if name not in cfg.tvs and not self._lock(name).locked():
                await self._controllers.pop(name).close()

    async def start_pairing(
        self, host: str, name: str, *, make_default: bool, replace: bool = False
    ) -> PairingJob:
        """Validate, then pair in the background so the caller is not blocked."""
        config.validate_name(name)
        ensure_can_add(self.load_config(), name, replace=replace)
        current = self._pairings.get(name)
        if current is not None and current.state == "waiting":
            raise InvalidInput(f"Already pairing '{name}'. Check its status instead.")
        # Register before awaiting so a second call cannot start a duplicate job.
        job = PairingJob(host=host)
        self._pairings[name] = job
        try:
            job.host = await validate_host_async(host)
            if not await asyncio.to_thread(reachable, job.host):
                raise Unreachable(f"No LG TV answered at {job.host}. Is it on and on this network?")
        except BaseException:
            del self._pairings[name]
            raise
        job.task = asyncio.create_task(self._pair(job, name, make_default, replace))
        return job

    def pairing_status(self, name: str) -> PairingJob:
        job = self._pairings.get(name)
        if job is None:
            raise NotFound(f"No pairing in progress or finished for '{name}'.")
        return job

    async def _pair(self, job: PairingJob, name: str, make_default: bool, replace: bool) -> None:
        try:
            entry = await pair(job.host, client_factory=self._factory)
            save_paired(
                name, entry, make_default=make_default, replace=replace, path=self.config_path
            )
        except LgtvError as err:
            job.state, job.message = "failed", str(err)
        except Exception:
            log.exception("Unexpected error while pairing '%s'", name)
            job.state, job.message = "failed", "Unexpected error while pairing. See server log."
        else:
            job.state, job.message = "paired", f"Paired TV '{name}' ({entry.model or 'LG TV'})."

    async def move(self, name: str, host: str) -> str:
        """Point a paired TV at a new address, after a person confirmed it."""
        new_host = await move_tv(name, host, self.config_path)
        async with self._lock(name):
            controller = self._controllers.pop(name, None)
            if controller is not None:
                await controller.close()
        return new_host

    async def close(self) -> None:
        for job in self._pairings.values():
            if job.task is not None and not job.task.done():
                job.task.cancel()
        controllers = list(self._controllers.values())
        self._controllers.clear()
        for controller in controllers:
            await controller.close()
