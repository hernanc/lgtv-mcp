# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-09-26

### Added

- MCP server `lgtv-mcp` (stdio) with 18 tools: discovery, pairing and address
  changes, status and info, apps, inputs, YouTube, volume, mute, power,
  screen, remote keys and on-screen messages.
- `lgtv` command-line tool with the same features and `--json` output.
- Multiple named TVs with a default, stored in an owner-only config file.
- Detection of a TV whose IP address changed, with a confirmation step
  (`lgtv move`, `set_tv_address`) before its key is sent to the new address.
- Wake-on-LAN and network standby wake-up.
- Import of the single-TV `key` and `host` files used by earlier scripts.
