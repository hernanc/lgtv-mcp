# Security policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub private vulnerability reporting](https://github.com/hernanc/lgtv-mcp/security/advisories/new).
Do not open a public issue.

Include what you found, how to reproduce it, and the impact you expect. You
should get a first reply within a week. Fixes are released as soon as they are
ready, and reporters are credited unless they prefer otherwise.

## Supported versions

Only the latest release receives security fixes.

## Scope and threat model

`lgtv-mcp` runs on a user's computer and controls TVs on the same local
network. In scope:

- Leaking the TV pairing key (it is a bearer credential for the TV).
- Making the tool connect to hosts outside the local network.
- Tool inputs or TV-provided data that cause unintended actions, code
  execution, or file access.
- Config file handling (permissions, parsing).

Out of scope: the LG webOS protocol itself (unauthenticated LAN traffic and
self-signed TLS are properties of the TV), and attackers who already control
the user's account or local network.
