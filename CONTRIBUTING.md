# Contributing

Thanks for helping. Bug reports, TV compatibility notes and pull requests are
all welcome.

## Setup

```sh
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check && uv run mypy
```

CI runs the same checks on Linux, macOS and Windows with Python 3.11 to 3.13.

## Layout

| Module | Responsibility |
|---|---|
| `config.py` | Paired TV registry (atomic, owner-only JSON file) |
| `discovery.py` | SSDP discovery, local-network host validation, Wake-on-LAN |
| `control.py` | `TvController` and `ControllerPool` over aiowebostv |
| `youtube.py`, `matching.py` | Pure helpers: deep links, name matching, text cleanup |
| `cli.py`, `server.py` | Thin front ends: the `lgtv` CLI and the `lgtv-mcp` server |

Features belong in `control.py` first, then get exposed by both front ends.

## Guidelines

- Tests need no TV: use the fake client in `tests/fakes.py`.
- Validate every input before it reaches the TV, and raise an `LgtvError`
  subclass with a message that tells the user what to do next.
- Never log, print or return the pairing key.
- Keep the MCP tool list small and each tool focused.

## Testing on a real TV

Before a release, run through this on a real TV:

1. `lgtv discover` lists the TV.
2. `lgtv pair <ip> --name test` shows a prompt; accepting it saves the TV.
3. `lgtv status`, `lgtv info`, `lgtv apps`, `lgtv inputs` look right.
4. `lgtv msg hello` shows a notification.
5. `lgtv yt dQw4w9WgXcQ` plays the video.
6. `lgtv vol 10`, `lgtv mute`, `lgtv unmute`, `lgtv input hdmi 1`.
7. `lgtv off`, wait, then `lgtv on` (needs Wake-on-LAN enabled on the TV).
8. From an MCP client: "what's on the TV?" and "show hello on the TV".

Please mention your TV model and webOS version in pull requests that touch TV
behavior.
