import socket

import pytest

from lgtv_mcp import discovery
from lgtv_mcp.discovery import Found, magic_packet, normalize_mac, parse_ssdp, validate_host
from lgtv_mcp.errors import InvalidInput

LG_REPLY = (
    b"HTTP/1.1 200 OK\r\n"
    b"Location: http://192.168.4.40:1031/\r\n"
    b"Cache-Control: max-age=1800\r\n"
    b"Server: WebOS/4.1.0 UPnP/1.0\r\n"
    b"EXT: \r\n"
    b"USN: uuid:3f2b8c1e-5d4a-4e6b-9c7d-0a1b2c3d4e5f::urn:lge-com:service:webos-second-screen:1\r\n"
    b"ST: urn:lge-com:service:webos-second-screen:1\r\n\r\n"
)


def test_parse_lg_reply() -> None:
    found = parse_ssdp(LG_REPLY, "192.168.4.40")
    assert found == Found(
        host="192.168.4.40",
        uuid="3f2b8c1e-5d4a-4e6b-9c7d-0a1b2c3d4e5f",
        server="WebOS/4.1.0 UPnP/1.0",
    )


def test_parse_ignores_other_devices() -> None:
    reply = (
        b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\nSERVER: eeroOS/latest\r\nUSN: uuid:x\r\n\r\n"
    )
    assert parse_ssdp(reply, "192.168.4.1") is None


def test_parse_ignores_non_ok_and_garbage() -> None:
    assert parse_ssdp(b"NOTIFY * HTTP/1.1\r\n\r\n", "192.168.4.2") is None
    assert parse_ssdp(b"\xff\xfe\x00garbage", "192.168.4.2") is None
    assert parse_ssdp(b"", "192.168.4.2") is None


def test_parse_ignores_public_source() -> None:
    assert parse_ssdp(LG_REPLY, "8.8.8.8") is None


def test_parse_cleans_server_header() -> None:
    reply = LG_REPLY.replace(b"WebOS/4.1.0 UPnP/1.0", b"WebOS\x1b[31m evil")
    found = parse_ssdp(reply, "192.168.4.40")
    assert found is not None
    assert "\x1b" not in found.server


@pytest.mark.parametrize(
    "host",
    ["192.168.4.40", "10.0.0.5", "172.16.3.4", "172.31.255.1", "127.0.0.1", "169.254.1.2"],
)
def test_validate_host_accepts_local(host: str) -> None:
    assert validate_host(host) == host


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "1.1.1.1",
        "172.32.0.1",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "169.254.169.254",
        "::",
        "::1",
        "fe80::1",
        "fe80::1%en0",
        "fd00::5",
        "2001::1",
        "2606:4700::1111",
    ],
)
def test_validate_host_rejects_public_and_special(host: str) -> None:
    with pytest.raises(InvalidInput):
        validate_host(host)


@pytest.mark.parametrize("host", ["", "a b", "http://192.168.1.2", "192.168.1.2:3000", "x" * 300])
def test_validate_host_rejects_malformed(host: str) -> None:
    with pytest.raises(InvalidInput):
        validate_host(host)


def test_validate_host_resolves_names_to_local_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("192.168.4.40", 0))]
    )
    assert validate_host("lgtv.local") == "192.168.4.40"


def test_validate_host_rejects_names_resolving_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]
    )
    with pytest.raises(InvalidInput, match="local network"):
        validate_host("example.com")


def test_validate_host_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*a: object, **k: object) -> None:
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(InvalidInput, match="resolve"):
        validate_host("nowhere.invalid")


def test_ipv4_mapped_ipv6_is_unwrapped() -> None:
    assert validate_host("::ffff:192.168.1.5") == "192.168.1.5"
    with pytest.raises(InvalidInput):
        validate_host("::ffff:8.8.8.8")


def test_validate_host_requires_all_resolved_addresses_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, 0, 0, "", ("192.168.4.40", 0)),
            (socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)),
        ],
    )
    with pytest.raises(InvalidInput):
        validate_host("mixed.example")


def test_validate_host_resolves_ipv4_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake(*a: object, **k: object) -> list[tuple[object, ...]]:
        seen.update(k)
        return [(socket.AF_INET, 0, 0, "", ("192.168.4.40", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    validate_host("tv.local")
    assert seen["family"] == socket.AF_INET


def test_parse_ignores_ipv6_source() -> None:
    assert parse_ssdp(LG_REPLY, "fe80::1") is None


@pytest.mark.parametrize(
    "mac", ["02:ab:cd:00:00:01", "02:AB:CD:00:00:01", "02-ab-cd-00-00-01", "02ab.cd00.0001"]
)
def test_normalize_mac(mac: str) -> None:
    assert normalize_mac(mac) == "02:ab:cd:00:00:01"


@pytest.mark.parametrize("mac", ["", "02:ab:cd:00:00", "zz:ab:cd:00:00:01", "02:ab:cd:00:00:01:00"])
def test_normalize_mac_rejects(mac: str) -> None:
    with pytest.raises(InvalidInput):
        normalize_mac(mac)


def test_magic_packet() -> None:
    packet = magic_packet("02:ab:cd:00:00:01")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:12] == bytes.fromhex("02abcd000001")


class FakeSocket:
    sent: list[tuple[bytes, tuple[str, int]]]

    def __init__(self, *a: object) -> None:
        FakeSocket.sent = []

    def __enter__(self) -> "FakeSocket":
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def setsockopt(self, *a: object) -> None:
        pass

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        FakeSocket.sent.append((data, addr))


def test_wake_on_lan_sends_broadcast_and_unicast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery.socket, "socket", FakeSocket)
    discovery.wake_on_lan(["02:ab:cd:00:00:01", "02:ab:cd:00:00:02"], "192.168.4.40")
    targets = {addr for _, addr in FakeSocket.sent}
    assert targets == {("255.255.255.255", 9), ("192.168.4.40", 9)}
    assert len({data for data, _ in FakeSocket.sent}) == 2


def test_wake_on_lan_requires_a_mac() -> None:
    with pytest.raises(InvalidInput, match="MAC"):
        discovery.wake_on_lan([], "192.168.4.40")
