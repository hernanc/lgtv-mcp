"""Find TVs on the local network, check reachability and wake them up.

Everything here is restricted to the local network: hosts must be IPv4
addresses in private, link-local or loopback ranges, and SSDP ``LOCATION``
URLs are never fetched, so a model cannot steer the tool to an internet host.
"""

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass

from .errors import InvalidInput, Unreachable
from .matching import clean_text

SSDP_ADDR = ("239.255.255.250", 1900)
WEBOS_ST = "urn:lge-com:service:webos-second-screen:1"
WEBOS_PORTS = (3000, 3001)  # plain WebSocket, then TLS (which aiowebostv falls back to)
WOL_PORT = 9
_MAX_DATAGRAM = 4096
_HOSTNAME = re.compile(r"(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9-]+)*\.?")
_MAC = re.compile(r"[0-9a-f]{12}")

# An explicit allowlist, because ipaddress.is_private has changed meaning
# between Python patch releases. webOS TVs are reached over IPv4 only.
_LOCAL_NETWORKS = tuple(
    ipaddress.IPv4Network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")
)
_BLOCKED = frozenset({ipaddress.IPv4Address("169.254.169.254")})  # cloud metadata service


@dataclass(frozen=True)
class Found:
    host: str
    uuid: str | None
    server: str


def discover(timeout: float = 3.0) -> list[Found]:
    """Search for LG webOS TVs with SSDP. Blocking; run in a thread from async code."""
    request = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {WEBOS_ST}\r\n\r\n"
    ).encode()
    found: dict[str, Found] = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            for _ in range(2):  # UDP is lossy; a second probe is cheap
                sock.sendto(request, SSDP_ADDR)
            deadline = time.monotonic() + timeout
            while (remaining := deadline - time.monotonic()) > 0:
                sock.settimeout(remaining)
                try:
                    data, (host, _port) = sock.recvfrom(_MAX_DATAGRAM)
                except TimeoutError:
                    break
                except OSError:  # one bad datagram (Windows raises on oversized ones)
                    continue
                if host not in found and (tv := parse_ssdp(data, host)):
                    found[host] = tv
    except OSError as err:
        raise Unreachable(
            f"Cannot search the local network ({err.strerror or err}). Check that this "
            "computer is on the TV's network; on macOS, allow Local Network access for "
            "this app in System Settings."
        ) from err
    return sorted(found.values(), key=lambda f: ipaddress.IPv4Address(f.host))


def parse_ssdp(data: bytes, host: str) -> Found | None:
    """Return a Found for an SSDP reply from a webOS TV on the local network."""
    if local_ipv4(host) is None:
        return None
    text = data[:_MAX_DATAGRAM].decode("utf-8", errors="replace")
    lines = text.split("\r\n")
    if not lines or not lines[0].startswith("HTTP/1.1 200"):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    server = headers.get("server", "")
    if headers.get("st") != WEBOS_ST and "webos" not in server.lower():
        return None
    usn = headers.get("usn", "")
    uuid = usn.removeprefix("uuid:").split("::", 1)[0] if usn.startswith("uuid:") else None
    return Found(host=host, uuid=clean_text(uuid) or None, server=clean_text(server))


def reachable(host: str, timeout: float = 1.5) -> bool:
    """True if the TV accepts TCP connections on a control port. Blocking.

    The TLS port is only tried when the plain one is refused (firmware that
    serves only TLS), so a TV that is off costs a single timeout.
    """
    for port in WEBOS_PORTS:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except ConnectionRefusedError:
            continue
        except OSError:
            return False
    return False


def validate_host(host: str) -> str:
    """Return the host as an IPv4 address string, if it is on the local network.

    Hostnames are resolved (blocking) and the address is returned, so later
    connections go to the checked address and cannot be redirected by DNS.
    Every address a hostname resolves to must be local.
    """
    host = host.strip()
    if (ip := _parse_ipv4(host)) is not None:
        if not _is_local(ip):
            raise InvalidInput(
                f"{host} is not a local network address. Only private (10.x, 172.16-31.x, "
                "192.168.x), link-local or loopback IPv4 addresses are allowed."
            )
        return str(ip)

    if not _HOSTNAME.fullmatch(host) or ":" in host:
        raise InvalidInput("Expected a TV IPv4 address or hostname, like 192.168.1.20.")
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as err:
        raise InvalidInput(f"Cannot resolve host '{host}'.") from err
    addresses = [ipaddress.IPv4Address(info[4][0]) for info in infos]
    if not addresses or not all(_is_local(a) for a in addresses):
        raise InvalidInput(f"Host '{host}' does not resolve to the local network.")
    return str(addresses[0])


async def validate_host_async(host: str) -> str:
    """validate_host without blocking the event loop on DNS."""
    return await asyncio.to_thread(validate_host, host)


def normalize_mac(mac: str) -> str:
    digits = re.sub(r"[:\-.]", "", mac.strip().lower())
    if not _MAC.fullmatch(digits):
        raise InvalidInput(f"Invalid MAC address: '{clean_text(mac)}'.")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + bytes.fromhex(normalize_mac(mac).replace(":", "")) * 16


def wake_on_lan(macs: list[str], host: str | None = None) -> None:
    """Send Wake-on-LAN magic packets to the broadcast address and the last known IP.

    A failed send (no broadcast route, or macOS reporting the powered-off TV's
    IP as down) does not stop the others; only failing every send is an error.
    """
    packets = [magic_packet(mac) for mac in macs]
    if not packets:
        raise InvalidInput("No MAC address saved for this TV, so it cannot be woken up.")
    targets = [("255.255.255.255", WOL_PORT)]
    if host:
        targets.append((validate_host(host), WOL_PORT))
    sent, failure = 0, None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for packet in packets:
            for target in targets:
                try:
                    sock.sendto(packet, target)
                    sent += 1
                except OSError as err:
                    failure = err
    if not sent and failure is not None:
        raise Unreachable(
            f"Could not send Wake-on-LAN ({failure.strerror or failure})."
        ) from failure


def _parse_ipv4(value: str) -> ipaddress.IPv4Address | None:
    """Parse a dotted IPv4 address, unwrapping IPv4-mapped IPv6 (::ffff:a.b.c.d)."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        return ip.ipv4_mapped
    return ip


def local_ipv4(value: str) -> ipaddress.IPv4Address | None:
    """The address if ``value`` is an allowlisted IPv4 literal. Never resolves names."""
    ip = _parse_ipv4(value)
    return ip if ip is not None and _is_local(ip) else None


def _is_local(ip: ipaddress.IPv4Address) -> bool:
    return ip not in _BLOCKED and any(ip in net for net in _LOCAL_NETWORKS)
