"""`lgtv`: control LG webOS TVs from the command line."""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from . import __version__, config
from .control import ControllerPool, TvController, ensure_can_add, pair, save_paired
from .discovery import discover
from .errors import InvalidInput, LgtvError

EXAMPLES = """\
examples:
  lgtv discover                   find TVs on the network
  lgtv pair 192.168.1.20 --name living
  lgtv status
  lgtv msg "Dinner is ready"
  lgtv app netflix
  lgtv yt https://youtu.be/dQw4w9WgXcQ
  lgtv vol 15
  lgtv key home down enter
  lgtv --tv bedroom off
  lgtv move living 192.168.1.35   after the TV's address changed
"""

Handler = Callable[[argparse.Namespace, ControllerPool], Awaitable[tuple[Any, str]]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lgtv",
        description="Control LG webOS TVs on your local network.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("-V", "--version", action="version", version=f"lgtv {__version__}")
    parser.add_argument("--tv", metavar="NAME", help="TV to control (default: the default TV)")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug details to stderr")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def add(name: str, handler: Handler, help_text: str) -> argparse.ArgumentParser:
        cmd = sub.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
        cmd.set_defaults(handler=handler)
        return cmd

    add("discover", cmd_discover, "find LG TVs on the local network")
    p = add("pair", cmd_pair, "pair with a TV (accept the prompt on the TV)")
    p.add_argument("host", help="TV IP address, from `lgtv discover`")
    p.add_argument("--name", default="tv", help="name for this TV (default: tv)")
    p.add_argument("--default", action="store_true", help="make it the default TV")
    p.add_argument("--replace", action="store_true", help="overwrite a TV with the same name")
    p = add("move", cmd_move, "confirm a paired TV's new IP address")
    p.add_argument("name")
    p.add_argument("host")
    add("list", cmd_list, "list paired TVs")
    add("default", cmd_default, "set the default TV").add_argument("name")
    add("remove", cmd_remove, "forget a paired TV").add_argument("name")

    add("status", cmd_status, "what the TV is showing, volume and power")
    add("info", cmd_info, "model, webOS version and address")
    add("apps", cmd_apps, "list installed apps")
    add("inputs", cmd_inputs, "list inputs")
    add("app", cmd_app, "open an app by name").add_argument("name", nargs="+")
    add("input", cmd_input, "switch input by name").add_argument("name", nargs="+")
    add("yt", cmd_yt, "play a YouTube video (URL or id)").add_argument("video")
    add("vol", cmd_vol, "show or set volume: 0-100, up or down").add_argument("level", nargs="?")
    add("mute", cmd_mute, "mute")
    add("unmute", cmd_unmute, "unmute")
    add("screen", cmd_screen, "turn the picture on or off").add_argument(
        "state", choices=["on", "off"]
    )
    add("on", cmd_on, "turn the TV on")
    add("off", cmd_off, "turn the TV off")
    add("key", cmd_key, "press remote buttons, e.g. home down enter").add_argument(
        "keys", nargs="+"
    )
    add("msg", cmd_msg, "show a message on the TV").add_argument("text", nargs="+")
    return parser


# Every handler returns (data, text): --json prints data, otherwise text is printed.
Output = tuple[Any, str]


def said(message: str) -> Output:
    return {"message": message}, message


# --- setup commands ------------------------------------------------------------------


async def cmd_discover(args: argparse.Namespace, pool: ControllerPool) -> Output:
    print("Searching for LG TVs...", file=sys.stderr)
    cfg = pool.load_config()
    names = {e.uuid: n for n, e in cfg.tvs.items() if e.uuid}
    found = await asyncio.to_thread(discover)
    rows = [
        {"host": f.host, "server": f.server, "paired_as": names.get(f.uuid) if f.uuid else None}
        for f in found
    ]
    lines = [
        f"{r['host']:<16} {r['server']}"
        + (f"  (paired as '{r['paired_as']}')" if r["paired_as"] else "")
        for r in rows
    ]
    return rows, "\n".join(lines) or "No LG TVs found. Make sure the TV is on and on this network."


async def cmd_pair(args: argparse.Namespace, pool: ControllerPool) -> Output:
    config.validate_name(args.name)
    ensure_can_add(pool.load_config(), args.name, replace=args.replace)
    print("Accept the connection prompt on the TV with the remote...", file=sys.stderr)
    entry = await pair(args.host)
    cfg = save_paired(
        args.name, entry, make_default=args.default, replace=args.replace, path=pool.config_path
    )
    default = " (default)" if cfg.default == args.name else ""
    return said(f"Paired '{args.name}': {entry.model or 'LG TV'} at {entry.host}{default}.")


async def cmd_move(args: argparse.Namespace, pool: ControllerPool) -> Output:
    host = await pool.move(args.name, args.host)
    return said(f"TV '{args.name}' now uses {host}.")


async def cmd_list(args: argparse.Namespace, pool: ControllerPool) -> Output:
    cfg = pool.load_config()
    rows = [
        {"name": n, "host": e.host, "model": e.model, "default": n == cfg.default}
        for n, e in sorted(cfg.tvs.items())
    ]
    lines = [
        f"{'*' if n == cfg.default else ' '} {n:<16} {e.host:<16} {e.model or ''}".rstrip()
        for n, e in sorted(cfg.tvs.items())
    ]
    return rows, "\n".join(lines) or "No TVs paired. Run: lgtv discover, then lgtv pair HOST"


async def cmd_default(args: argparse.Namespace, pool: ControllerPool) -> Output:
    def apply(cfg: config.Config) -> None:
        cfg.get(args.name)
        cfg.default = args.name

    config.update(apply, pool.config_path)
    return said(f"Default TV is now '{args.name}'.")


async def cmd_remove(args: argparse.Namespace, pool: ControllerPool) -> Output:
    config.update(lambda c: c.remove(args.name), pool.config_path)
    return said(f"Removed '{args.name}' from this computer.")


# --- TV commands ---------------------------------------------------------------------


async def on_tv(
    args: argparse.Namespace,
    pool: ControllerPool,
    operation: Callable[[TvController], Awaitable[Any]],
    **options: Any,
) -> Any:
    return await pool.run(args.tv, operation, **options)


def read(fn: Callable[[TvController], Any]) -> Callable[[TvController], Awaitable[Any]]:
    async def run(controller: TvController) -> Any:
        return fn(controller)

    return run


async def cmd_status(args: argparse.Namespace, pool: ControllerPool) -> Output:
    s = await on_tv(args, pool, read(lambda c: c.status()))
    lines = [f"Power:   {s['power']}", f"Showing: {s['app'] or 'home screen'}"]
    if s.get("channel"):
        program = f"  ({s['program']})" if s.get("program") else ""
        lines.append(f"Channel: {s['channel']}{program}")
    muted = " (muted)" if s["muted"] else ""
    lines.append(f"Volume:  {s['volume']}{muted}  output: {s['sound_output']}")
    return s, "\n".join(lines)


async def cmd_info(args: argparse.Namespace, pool: ControllerPool) -> Output:
    info = await on_tv(args, pool, read(lambda c: c.info()))
    lines = [
        f"Model:  {info['model']}",
        f"webOS:  {info['webos_version']}",
        f"Host:   {info['host']}",
        f"MACs:   {', '.join(info['macs']) or '-'}",
    ]
    return info, "\n".join(lines)


async def cmd_apps(args: argparse.Namespace, pool: ControllerPool) -> Output:
    apps = await on_tv(args, pool, read(lambda c: c.apps()))
    width = max((len(a["title"]) for a in apps), default=0)
    return apps, "\n".join(f"{a['title']:<{width}}  {a['id']}" for a in apps)


async def cmd_inputs(args: argparse.Namespace, pool: ControllerPool) -> Output:
    inputs = await on_tv(args, pool, read(lambda c: c.inputs()))
    width = max((len(i["label"]) for i in inputs), default=0)
    lines = [f"{'*' if i['connected'] else ' '} {i['label']:<{width}}  {i['id']}" for i in inputs]
    return inputs, "\n".join(lines)


async def cmd_app(args: argparse.Namespace, pool: ControllerPool) -> Output:
    title = await on_tv(args, pool, lambda c: c.launch(" ".join(args.name)))
    return said(f"Opened {title}.")


async def cmd_input(args: argparse.Namespace, pool: ControllerPool) -> Output:
    label = await on_tv(args, pool, lambda c: c.switch_input(" ".join(args.name)))
    return said(f"Switched to {label}.")


async def cmd_yt(args: argparse.Namespace, pool: ControllerPool) -> Output:
    target = await on_tv(args, pool, lambda c: c.youtube(args.video))
    return {"message": "Playing on YouTube.", "target": target}, "Playing on YouTube."


async def cmd_vol(args: argparse.Namespace, pool: ControllerPool) -> Output:
    level = args.level
    if level is None:
        s = await on_tv(args, pool, read(lambda c: c.status()))
        return {"volume": s["volume"], "muted": s["muted"]}, (
            f"{s['volume']}{' (muted)' if s['muted'] else ''}"
        )
    if level in ("up", "down"):
        await on_tv(args, pool, lambda c: c.step_volume(level), retry=False)
        return said(f"Volume {level}.")
    if level.isdecimal():
        await on_tv(args, pool, lambda c: c.set_volume(int(level)))
        return said(f"Volume set to {int(level)}.")
    raise InvalidInput("Volume must be 0-100, up or down.")


async def cmd_mute(args: argparse.Namespace, pool: ControllerPool) -> Output:
    await on_tv(args, pool, lambda c: c.set_mute(True))
    return said("Muted.")


async def cmd_unmute(args: argparse.Namespace, pool: ControllerPool) -> Output:
    await on_tv(args, pool, lambda c: c.set_mute(False))
    return said("Unmuted.")


async def cmd_screen(args: argparse.Namespace, pool: ControllerPool) -> Output:
    await on_tv(args, pool, lambda c: c.set_screen(args.state == "on"))
    return said(f"Screen {args.state}.")


async def cmd_on(args: argparse.Namespace, pool: ControllerPool) -> Output:
    return said(await on_tv(args, pool, lambda c: c.power_on(), connect=False))


async def cmd_off(args: argparse.Namespace, pool: ControllerPool) -> Output:
    changed = await on_tv(args, pool, lambda c: c.power_off())
    return said("Turned the TV off." if changed else "The TV is already off.")


async def cmd_key(args: argparse.Namespace, pool: ControllerPool) -> Output:
    pressed = await on_tv(args, pool, lambda c: c.press(args.keys), retry=False)
    return {"pressed": pressed}, f"Pressed {' '.join(k.lower() for k in pressed)}."


async def cmd_msg(args: argparse.Namespace, pool: ControllerPool) -> Output:
    shown = await on_tv(args, pool, lambda c: c.toast(" ".join(args.text)))
    return said(f"Shown: {shown}")


# --- entry point ---------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> tuple[Any, str]:
    pool = ControllerPool(config.config_path())
    try:
        result: tuple[Any, str] = await args.handler(args, pool)
        return result
    finally:
        await pool.close()


def _normalize_argv(argv: list[str]) -> list[str]:
    """Let `lgtv yt -8DNbZkeRTs` work: YouTube ids may start with '-'."""
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] == "--tv" else 1  # skip global options (and --tv's value)
    rest = argv[i + 1 :]
    if (
        argv[i : i + 1] == ["yt"]
        and len(rest) == 1
        and rest[0].startswith("-")
        and rest[0] not in ("-h", "--help")
    ):
        return [*argv[: i + 1], "--", rest[0]]
    return argv


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(_normalize_argv(list(sys.argv[1:] if argv is None else argv)))
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        data, text = asyncio.run(_run(args))
    except LgtvError as err:
        sys.exit(f"lgtv: {err}")
    except KeyboardInterrupt:
        sys.exit(130)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif text:
        print(text)


if __name__ == "__main__":
    main()
