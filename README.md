# lgtv-mcp

Control LG webOS TVs from Claude, Cursor or any other MCP client, and from the
command line.

> "Put the news on mute and open YouTube."
> "What's on the TV right now?"
> "Play https://youtu.be/dQw4w9WgXcQ on the bedroom TV."
> "Show 'Dinner is ready' on the TV."

It talks to the TV directly over your local network. No cloud account, no LG
app, no Home Assistant required.

[![CI](https://github.com/hernanc/lgtv-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/hernanc/lgtv-mcp/actions/workflows/ci.yml)

## Requirements

- An LG TV running webOS (most LG smart TVs from 2014 on). Tested on webOS 4.
- The computer running `lgtv-mcp` must be on the **same local network** as the
  TV. It cannot run in a cloud sandbox.
- [uv](https://docs.astral.sh/uv/) (installs Python 3.11+ for you if needed).

## Set up with an MCP client

The server runs on demand through `uvx`; nothing to install first.

**Claude Code**

```sh
claude mcp add lgtv -- uvx --from git+https://github.com/hernanc/lgtv-mcp lgtv-mcp
```

**Claude Desktop**: edit `claude_desktop_config.json` (Settings, Developer,
Edit Config):

```json
{
  "mcpServers": {
    "lgtv": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/hernanc/lgtv-mcp", "lgtv-mcp"]
    }
  }
}
```

**Cursor**: add the same `mcpServers` block to `~/.cursor/mcp.json`.

To pin a release, use `git+https://github.com/hernanc/lgtv-mcp@v0.1.0`.

### First run: pairing

Ask your assistant to "find and pair my LG TV". It will call `discover_tvs`
and `pair_tv`, and a prompt will appear on the TV. **Accept it with the remote
within a minute.** The pairing is saved and never needs repeating.

You can also pair from a terminal (see below), and the MCP server picks it up.

## Tools

Every tool except the setup ones takes an optional `tv` name; the default TV is
used when it is omitted.

| Tool | What it does |
|---|---|
| `discover_tvs` | Find LG TVs on the network |
| `pair_tv`, `pair_tv_status` | Pair a TV (someone accepts the prompt on screen) |
| `list_tvs` | Paired TVs and the default |
| `set_tv_address` | Confirm a paired TV's new IP address |
| `get_status` | Power, current app or input, channel and program, volume |
| `get_tv_info` | Model, webOS version, address |
| `list_apps`, `list_inputs` | Installed apps, HDMI and other inputs |
| `launch_app` | Open an app by name: "netflix", "prime", "youtube" |
| `switch_input` | Switch input: "HDMI 2", or its custom label like "PS5" |
| `play_youtube` | Play a video from any YouTube URL or id, honoring `t=` |
| `set_volume`, `set_mute` | Volume 0 to 100 or one step up/down; mute |
| `power` | On (network wake or Wake-on-LAN) or off |
| `set_screen` | Picture off while sound keeps playing |
| `press_keys` | Remote buttons: home, back, arrows, enter, play, pause... (power is left to `power`) |
| `show_message` | Show a notification on the TV |

Read-only tools are annotated as such, and `power`, `pair_tv` and
`set_tv_address` are marked destructive, so clients can auto-approve reads and
ask before those.

## Command line

Install the `lgtv` command (and `lgtv-mcp`) once:

```sh
uv tool install git+https://github.com/hernanc/lgtv-mcp
```

```sh
lgtv discover                          # find TVs
lgtv pair 192.168.1.20 --name living   # accept the prompt on the TV
lgtv status
lgtv msg "Dinner is ready"
lgtv app netflix
lgtv input hdmi 2
lgtv yt "https://youtu.be/dQw4w9WgXcQ?t=42"
lgtv yt dQw4w9WgXcQ
lgtv vol 15          # or: lgtv vol up / lgtv vol down / lgtv vol
lgtv key home down enter
lgtv screen off
lgtv off
lgtv on
lgtv --tv bedroom mute
lgtv --json status   # machine-readable output (works for every command)
```

Run `lgtv --help` or `lgtv <command> --help` for everything else (`list`,
`default`, `remove`, `move`, `apps`, `inputs`, `info`, `unmute`). To pair a
TV again under an existing name, add `--replace`.

## Turning the TV on

A TV that is off drops off the network, so `power on` uses Wake-on-LAN. Enable
it on the TV once: **Settings, General, Devices, TV Management, Turn on via
Wi-Fi** (named "Mobile TV On" or "LG Connect Apps" on some models). With
"Quick Start+" enabled the TV also stays reachable in standby and wakes faster.

## Configuration

Paired TVs are stored in `~/.config/lgtv/config.json` (`%APPDATA%\lgtv\` on
Windows, or `$XDG_CONFIG_HOME/lgtv/`). Set `LGTV_CONFIG` to use another file.

**If the TV's IP address changes**, commands report where a device claiming to
be the TV now answers, and ask you to confirm it (`lgtv move NAME NEW_IP`, or
approve the `set_tv_address` tool). The address is not updated silently,
because any device on the network can claim to be the TV and would receive
its pairing key. A DHCP reservation for the TV on your router avoids this
altogether.

## Security

- **The pairing key is a password for your TV.** Anyone with it can control
  the TV from your network. It lives only in the config file, which is created
  with owner-only permissions (`0600`), and is never printed, logged or
  returned by any tool. `lgtv remove NAME` deletes it locally. (If you used
  an earlier single-TV script, its `key` and `host` files are imported but
  left in place, since that script may still need them; delete them once
  you no longer do.) Most webOS
  versions have no per-device revoke; resetting the TV to its initial
  settings clears every pairing.
- **Local network only.** The tool refuses to pair with or connect to any
  address outside the private IPv4 ranges (10.x, 172.16-31.x, 192.168.x),
  link-local or loopback, so a prompt cannot point it at an internet host.
  The connection never follows redirects, and the TV's own input socket must
  be in those ranges too. The MCP tools take IP addresses only, so a prompt
  cannot make the tool look up a name in DNS; on the command line, hostnames
  must resolve only to local addresses. SSDP location URLs are never fetched.
- **The key only goes where you said.** Pairing under an existing name needs
  an explicit `replace`, and a TV that changes address is never followed
  without your confirmation. A device at the saved address that reports a
  different id than the paired TV is refused.
- **Traffic to the TV is not authenticated.** LG TVs expose their control API
  over plain WebSocket (port 3000) or TLS with a self-signed certificate (port
  3001), so the connection cannot be verified. Use it on a network you trust.
- **TV data is untrusted.** App names, input labels and program titles come
  from the TV. They are cleaned of control characters, and the server tells
  the model to treat them as data, not instructions.
- All inputs are validated before reaching the TV (volume range, message
  length, known remote keys, YouTube hosts only).

Report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## Troubleshooting

**No TVs found.** The TV must be on and on the same network and subnet. Mesh
systems, guest networks and "AP isolation" often split devices into separate
networks (for example `192.168.4.x` vs `192.168.11.x`); check that your
computer's IP address and the TV's start the same way.

**macOS: "No route to host" or nothing found.** macOS asks before apps can
reach devices on the local network. Allow your terminal or MCP client in
**System Settings, Privacy & Security, Local Network**, then restart it.

**Pairing fails with "cancelled or timed out".** Someone needs to be at the TV
to accept the prompt within about a minute. If no prompt appears, enable "LG
Connect Apps" or "Mobile TV On" in the TV's network settings.

**"The TV denied permission for this action."** Some models restrict a few
features to LG's own apps; everything else keeps working.

**Debug output.** `lgtv --verbose status` logs the conversation with the TV to
stderr (the pairing key is not logged).

## Development

```sh
git clone https://github.com/hernanc/lgtv-mcp && cd lgtv-mcp
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check && uv run mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md). The TV protocol is handled by
[aiowebostv](https://github.com/home-assistant-libs/aiowebostv), the library
behind Home Assistant's LG integration.

## License

MIT. Not affiliated with or endorsed by LG Electronics. "LG" and "webOS" are
trademarks of their respective owners.
