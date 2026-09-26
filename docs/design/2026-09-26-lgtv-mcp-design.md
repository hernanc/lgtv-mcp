# lgtv-mcp: design

Status: approved, 2026-09-26

## Goal

A public, open-source Python package that lets people control LG webOS TVs on
their local network from two front ends:

- `lgtv-mcp`: an MCP server (stdio) so MCP clients such as Claude Desktop,
  Claude Code and Cursor can operate the TV through tools.
- `lgtv`: a command-line tool for humans and scripts with the same features.

It grows out of a private `tv` script that already works against a 50UM7360
(webOS 4.10). Nothing specific to that TV (IP, MAC, key) ships in the package.

### Success criteria

- A new user can install with one `uvx` command, discover and pair a TV, and
  control it from an MCP client without reading source code.
- Every feature is implemented once and shared by the CLI and MCP server.
- No credential, network detail or TV-provided string can be used to make the
  tool reach outside the local network or leak the pairing key.
- Unit and in-process MCP tests run in CI without a TV.

### Constraints

- Must run on the same LAN as the TV. It cannot work from a cloud sandbox.
- Python 3.11 or newer (floor set by `aiowebostv`).
- Published first as a public GitHub repo (`hernanc/lgtv-mcp`). PyPI and the
  MCP registry come later.

## Non-goals (v1)

Channel list and channel switching, on-screen text input, media casting,
pointer control, picture settings, HTTP/SSE transports, Windows service
integration. The architecture keeps these easy to add.

## Architecture

```
src/lgtv_mcp/
  __init__.py     version
  config.py       TV registry (load, save, migrate)
  discovery.py    SSDP search, reachability, Wake-on-LAN, host validation
  youtube.py      YouTube URL or id -> webOS deep link
  matching.py     fuzzy name matching for apps and inputs
  errors.py       LgtvError hierarchy with user-facing messages
  control.py      TvController: async API over aiowebostv
  cli.py          `lgtv` entry point (argparse)
  server.py       `lgtv-mcp` entry point (MCP 2.x MCPServer, stdio)
tests/
```

Dependencies: `aiowebostv` (Apache-2.0, Home Assistant's LG client), `mcp`
(MIT, official SDK, 2.x `MCPServer` API), plus `aiohttp` and `pydantic`, which
those already pull in but which we import directly and so declare.

### Units

**config.py**. A `Config` holds `default: str | None` and
`tvs: dict[str, TvEntry]`. `TvEntry` fields: `host`, `key`, `macs` (Wi-Fi and
wired, as reported by the TV), `uuid`, `model`. Stored as JSON at `$LGTV_CONFIG` if set, else
`$XDG_CONFIG_HOME/lgtv/config.json`, else `~/.config/lgtv/config.json`
(`%APPDATA%\lgtv\config.json` on Windows). Writes are atomic (temp file in the
same directory, `fsync`, `os.replace`). The directory is created `0700` and the
file `0600`. On load, a file readable by group or others triggers a warning on
stderr. TV names must match `^[a-z0-9][a-z0-9_-]{0,31}$`. One-time migration:
if `config.json` is missing and the legacy `key` and `host` files exist in the
same directory, import them as a TV named `tv` and make it the default. Legacy
files are left in place.

**discovery.py**.
- `discover(timeout) -> list[Found]`: SSDP M-SEARCH for
  `urn:lge-com:service:webos-second-screen:1`. Keeps responses whose headers
  identify webOS, records IP, `USN` UUID and `SERVER`. It never fetches the
  `LOCATION` URL, so a hostile device on the LAN cannot steer us to another
  host. Responses are capped at 4 KiB.
- `reachable(host) -> bool`: TCP connect to 3000 with a short timeout.
- `validate_host(host) -> str`: accepts an IPv4 literal (IPv4-mapped IPv6 is
  unwrapped) or a hostname resolved over IPv4, and requires every address to
  be in an explicit allowlist: 10/8, 172.16/12, 192.168/16, 169.254/16
  (except 169.254.169.254) and 127/8. `ipaddress.is_private` is not used
  because its meaning changed between Python patch releases. IPv6 is out of
  scope: webOS TVs are reached over IPv4. This stops a prompt injection from
  pointing `pair_tv` at an internet host. An async wrapper runs the DNS
  lookup in a thread.
- `wake_on_lan(macs, host)`: validates each MAC and sends the magic packet
  (UDP 9) to the limited broadcast `255.255.255.255` and unicast to the TV's
  last known IP. We do not guess a subnet directed broadcast.

**youtube.py**. `youtube_target(value) -> str`. Accepts watch, youtu.be,
shorts, live, embed and `/v/` URLs, and bare 11-character ids (including ones
starting with `-`). Only `youtube.com`, `*.youtube.com`, `youtu.be` and
`youtube-nocookie.com` hosts are accepted for URLs. Parses `t=` in seconds or
`1h2m3s` form. Output is always rebuilt from the parsed id and integer offset,
never by echoing user input.

**matching.py**. `pick(query, items: dict[id, label], kind) -> id`. Normalizes
to lowercase alphanumerics; exact id or label wins, then a unique substring
match. Zero or several matches raise `NotFound` / `Ambiguous` listing
candidates (capped at 10).

**errors.py**. `LgtvError(message)` base, subclasses: `NotConfigured`,
`UnknownTv`, `Unreachable`, `TvOff`, `PairingFailed`, `PermissionDenied`,
`NotFound`, `Ambiguous`, `InvalidInput`. Messages are short and tell the
caller what to do next.

**control.py**. `TvController(entry: TvEntry)` wraps one `WebOsClient`.
- `connect()`: use the saved host if reachable. Otherwise, if SSDP finds a
  device claiming the TV's UUID at another address, raise `TvMoved` and do
  not connect: the UUID is broadcast in the clear, so a hostile device could
  claim it and would receive the key. A person confirms the new address with
  `lgtv move` or the `set_tv_address` tool. Map library exceptions to
  `LgtvError`, and close the client on any failure, cancellation included.
- Operations: `status()`, `info()`, `apps()`, `inputs()`, `launch(name)`,
  `switch_input(name)`, `youtube(value)`, `set_volume(level)`,
  `step_volume(up)`, `set_mute(muted)`, `set_screen(on)`, `power_off()`,
  `press(keys)`, `toast(text)`. Return plain dataclasses or dicts.
- `pair(host) -> TvEntry`: connect without key, wait up to 60 s for the user
  to accept (aiowebostv's own limit is 10 s, so it is raised while pairing),
  read key, UUID, model and both MAC addresses (Wi-Fi and wired) from the
  TV. Wake-on-LAN targets both, since the TV does not say which is active.
- A `ControllerPool` (used by the MCP server) keeps one lazily connected
  controller per TV, reconnects and retries once on a dropped connection
  (never for key sequences or volume steps, which are unsafe to repeat),
  applies a per-call timeout (20 s, pairing 60 s) and closes everything on
  shutdown. A TV refusing a request is reported as such, not as a dropped
  connection. Pairing jobs are registered before any await, so a name cannot
  be paired twice at once, and an existing name is only overwritten with
  `replace`.
  The CLI creates a controller per invocation.

**cli.py** (`lgtv`). Subcommands: `discover`, `pair HOST [--name N]
[--default] [--replace]`, `move NAME HOST`, `list`, `default NAME`,
`remove NAME`, `status`, `info`, `apps`,
`inputs`, `app NAME`, `input NAME`, `yt URL_OR_ID`, `vol [N|up|down]`,
`mute`, `unmute`, `screen on|off`, `on`, `off`, `key KEY...`, `msg TEXT...`.
Global `--tv NAME`, `--json` (every command prints JSON) and `--version`;
option abbreviations are disabled. Errors print `lgtv: message` to stderr,
exit code 1. A `--` is inserted before a lone `yt` argument so ids starting
with `-` parse.

**server.py** (`lgtv-mcp`). `MCPServer` with a lifespan that owns the
`ControllerPool`. Tools, each with optional `tv: str | None`:

| Tool | Annotations |
|---|---|
| `list_tvs` | read-only |
| `discover_tvs` | read-only, open-world |
| `pair_tv(host, name, make_default=False, replace=False)` | starts background pairing, returns immediately |
| `pair_tv_status(name)` | read-only |
| `set_tv_address(name, host)` | destructive: sends the key to a new address |
| `get_status`, `get_tv_info`, `list_apps`, `list_inputs` | read-only |
| `power(state: "on" \| "off")` | destructive (off) |
| `set_screen(on: bool)` | |
| `set_volume(level: int 0..100 \| None, step: "up" \| "down" \| None)` | |
| `set_mute(muted: bool)` | |
| `switch_input(name)`, `launch_app(name)` | |
| `play_youtube(url_or_id)` | |
| `press_keys(keys: list[str], max 20)` | |
| `show_message(text: str, max 200 chars)` | |

`LgtvError` becomes `ToolError(message)`. Any other exception becomes a
generic "unexpected error" message; details go to the stderr log only.

## Data flow

```
MCP client --stdio--> server.py tool --> ControllerPool --> TvController
                                                    |            |
                                               config.py    aiowebostv --ws(s)--> TV :3000/:3001
```

## Security

- **Pairing key**: a bearer credential for the TV. Stored only in the config
  file (`0600`, directory `0700`), never logged, never returned by a tool or
  printed by the CLI (`list` shows `key: set`).
- **Transport**: `aiowebostv` uses `ws://` on 3000 and falls back to `wss://`
  on 3001 without certificate checks, because LG TVs use self-signed
  certificates. Traffic on the LAN is therefore not authenticated. The README
  says so plainly.
- **Network scope**: only allowlisted private, link-local or loopback IPv4
  hosts are accepted for pairing and connections. SSDP `LOCATION` URLs are
  never fetched. A moved TV is never followed without confirmation.
- **Input validation**: all tool and CLI inputs are validated before reaching
  the TV (names, lengths, key whitelist, volume range, MAC format, YouTube
  host whitelist).
- **Untrusted TV data**: app titles, input labels and program names come from
  the TV and are returned as data. Tool descriptions remind the model not to
  treat them as instructions. Control characters are stripped.
- **stdout discipline**: the MCP server logs only to stderr, since stdout is
  the protocol channel.
- **Supply chain**: `uv.lock` committed; GitHub Actions pinned to commit SHAs
  with least-privilege `permissions`; Dependabot for pip and actions;
  `SECURITY.md` with a private reporting path.

## Error handling

| Case | Message |
|---|---|
| No TV configured | No TV paired yet. Run discover_tvs, then pair_tv. |
| Unknown TV name | No TV named 'x'. Known: a, b. |
| Unreachable | TV 'x' is not reachable (last seen 1.2.3.4). It may be off or on another network. |
| Moved | TV 'x' is not at A, but a device claiming to be it answered at B. Confirm with lgtv move or set_tv_address. |
| Refused | The TV rejected the request. |
| TV off | TV 'x' is off. Use power on first. |
| Pairing cancelled | Pairing was cancelled or timed out on the TV. Retry while someone is at the TV. |
| 401 | The TV denied permission for this action. |
| Ambiguous name | 'play' matches several apps: A, B, C. |

## Testing

- pytest with `pytest-asyncio`. Unit tests for `config` (paths, perms,
  atomic write, migration, name validation), `discovery.validate_host`,
  SSDP parsing, MAC validation, `youtube`, `matching`.
- `TvController` tests against a fake `WebOsClient` (no network).
- MCP tests through the SDK's in-memory client: tool list, schemas,
  annotations, error mapping, a few end-to-end calls on the fake client.
- Ruff (lint and format) and mypy strict on `src/`.
- CI: GitHub Actions matrix on Python 3.11, 3.12, 3.13, on Linux, macOS and
  Windows.
- Manual smoke checklist in `CONTRIBUTING.md` for a real TV.

## Repository

MIT license, README (install for Claude Desktop, Claude Code, Cursor; CLI
usage; security notes; troubleshooting), CHANGELOG, CONTRIBUTING, SECURITY,
`.gitignore`, `pyproject.toml` (hatchling, src layout, entry points
`lgtv` and `lgtv-mcp`), GitHub Actions CI, Dependabot.

## Later

PyPI release, MCP registry listing, channel tools, casting.
