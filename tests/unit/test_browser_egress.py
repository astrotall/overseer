from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest

from libs.browser import PlaywrightBrowserBackend
from libs.browser.backend import EGRESS_LAUNCH_ARGS, PROXIED_LOOPBACK_OVERRIDE_ENV
from libs.browser.egress import (
    SOCKS_ATYP_DOMAIN,
    SOCKS_ATYP_IPV4,
    SOCKS_ATYP_IPV6,
    SOCKS_CMD_CONNECT,
    SOCKS_NO_ACCEPTABLE_METHODS,
    SOCKS_REPLY_COMMAND_NOT_SUPPORTED,
    SOCKS_REPLY_CONNECTION_REFUSED,
    SOCKS_REPLY_HOST_UNREACHABLE,
    SOCKS_REPLY_NOT_ALLOWED,
    SOCKS_REPLY_SUCCEEDED,
    EgressDeniedError,
    EgressGuard,
    EgressProxy,
    IPAddress,
    is_public_address,
)
from libs.core.exceptions import ConfigurationError

LOOPBACK = ipaddress.IPv4Address("127.0.0.1")
SOCKS_BIND = 2

FORBIDDEN_ADDRESSES = [
    "127.0.0.1",
    "127.255.255.254",
    "0.0.0.0",
    "10.0.0.1",
    "172.16.0.1",
    "172.18.0.2",
    "192.168.1.1",
    "100.64.0.1",
    "169.254.169.254",
    "192.0.2.1",
    "198.18.0.1",
    "224.0.0.1",
    "240.0.0.1",
    "255.255.255.255",
    "::",
    "::1",
    "fc00::1",
    "fd12:3456::1",
    "fe80::1",
    "fec0::1",
    "ff02::1",
    "100::1",
    "2001:db8::1",
    "2001::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::127.0.0.1",
    "2002:7f00:1::",
    "2002:a00:1::",
    "64:ff9b::7f00:1",
    "64:ff9b::a9fe:a9fe",
]

PUBLIC_ADDRESSES = [
    "8.8.8.8",
    "1.1.1.1",
    "93.184.215.14",
    "2606:4700:4700::1111",
    "2001:4860:4860::8888",
    "::ffff:8.8.8.8",
    "2002:808:808::",
    "64:ff9b::808:808",
]


class FakeResolver:
    def __init__(self, answers: dict[str, Sequence[str]]) -> None:
        self._answers = answers
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int) -> list[IPAddress]:
        self.calls.append(host)
        if host not in self._answers:
            raise OSError(f"неизвестное имя {host}")
        return [ipaddress.ip_address(address) for address in self._answers[host]]


class EchoServer:
    def __init__(self) -> None:
        self.connections = 0
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def aclose(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        data = await reader.read(1024)
        writer.write(data)
        await writer.drain()
        writer.close()


@pytest.fixture
async def echo_server() -> AsyncIterator[EchoServer]:
    server = EchoServer()
    await server.start()
    yield server
    await server.aclose()


@pytest.fixture
async def closed_port() -> int:
    server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    server.close()
    await server.wait_closed()
    return port


@asynccontextmanager
async def running(guard: EgressGuard) -> AsyncIterator[EgressProxy]:
    proxy = EgressProxy(guard)
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.aclose()


async def _open(proxy: EgressProxy) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    host, port = proxy.server_url.removeprefix("socks5://").rsplit(":", 1)
    return await asyncio.open_connection(host, int(port))


async def socks_connect(
    proxy: EgressProxy,
    address_type: int,
    host: str,
    port: int,
    *,
    command: int = SOCKS_CMD_CONNECT,
) -> tuple[int, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await _open(proxy)
    writer.write(bytes([5, 1, 0]))
    await writer.drain()
    assert await reader.readexactly(2) == bytes([5, 0])

    if address_type == SOCKS_ATYP_IPV4:
        encoded = ipaddress.IPv4Address(host).packed
    elif address_type == SOCKS_ATYP_IPV6:
        encoded = ipaddress.IPv6Address(host).packed
    else:
        encoded = bytes([len(host)]) + host.encode()
    writer.write(bytes([5, command, 0, address_type]) + encoded + port.to_bytes(2, "big"))
    await writer.drain()

    reply = await reader.readexactly(10)
    return reply[1], reader, writer


@pytest.mark.parametrize("address", FORBIDDEN_ADDRESSES)
def test_internal_and_special_purpose_addresses_are_not_public(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize("address", PUBLIC_ADDRESSES)
def test_ordinary_internet_addresses_are_public(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize(
    "answers",
    [["127.0.0.1"], ["::1"], ["fd12:3456::1"], ["172.18.0.2"], ["93.184.215.14", "10.0.0.5"]],
    ids=["loopback", "ipv6-loopback", "ipv6-unique-local", "docker-network", "mixed-answer"],
)
async def test_a_name_that_resolves_into_the_internal_network_is_denied(
    answers: list[str],
) -> None:
    guard = EgressGuard(resolver=FakeResolver({"evil.test": answers}))

    with pytest.raises(EgressDeniedError):
        await guard.resolve("evil.test", 443)


async def test_a_name_that_resolves_to_public_addresses_is_allowed() -> None:
    answers = ["93.184.215.14", "2606:4700:4700::1111"]
    guard = EgressGuard(resolver=FakeResolver({"example.test": answers}))

    addresses = await guard.resolve("example.test", 443)

    assert addresses == [ipaddress.ip_address(address) for address in answers]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://0.0.0.0:8080/",
        "http://[::1]:8080/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
async def test_ip_literals_are_checked_without_asking_the_resolver(url: str) -> None:
    resolver = FakeResolver({})
    guard = EgressGuard(resolver=resolver)

    with pytest.raises(EgressDeniedError):
        await guard.check_url(url)

    assert resolver.calls == []


async def test_an_exemption_covers_exactly_one_endpoint() -> None:
    guard = EgressGuard(exempt={(LOOPBACK, 8080)})

    await guard.resolve("127.0.0.1", 8080)
    with pytest.raises(EgressDeniedError):
        await guard.resolve("127.0.0.1", 8081)


async def test_check_url_applies_the_default_port_of_the_scheme() -> None:
    guard = EgressGuard(
        resolver=FakeResolver({"local.test": ["127.0.0.1"]}), exempt={(LOOPBACK, 80)}
    )

    await guard.check_url("http://local.test/")
    with pytest.raises(EgressDeniedError):
        await guard.check_url("https://local.test/")


async def test_a_name_that_does_not_resolve_is_a_network_error_not_a_denial() -> None:
    guard = EgressGuard(resolver=FakeResolver({}))

    with pytest.raises(OSError, match="неизвестное имя"):
        await guard.resolve("missing.test", 443)


async def test_an_empty_answer_is_never_treated_as_allowed() -> None:
    guard = EgressGuard(resolver=FakeResolver({"empty.test": []}))

    with pytest.raises(OSError, match="ни в один адрес"):
        await guard.resolve("empty.test", 443)


async def test_the_proxy_tunnels_to_the_address_it_checked_and_resolves_once(
    echo_server: EchoServer,
) -> None:
    resolver = FakeResolver({"site.test": ["127.0.0.1"]})
    guard = EgressGuard(resolver=resolver, exempt={(LOOPBACK, echo_server.port)})

    async with running(guard) as proxy:
        code, reader, writer = await socks_connect(
            proxy, SOCKS_ATYP_DOMAIN, "site.test", echo_server.port
        )
        writer.write(b"ping")
        await writer.drain()
        echoed = await reader.read(4)
        writer.close()

    assert code == SOCKS_REPLY_SUCCEEDED
    assert echoed == b"ping"
    assert resolver.calls == ["site.test"]
    assert echo_server.connections == 1


@pytest.mark.parametrize(
    ("address_type", "host"),
    [
        (SOCKS_ATYP_IPV4, "127.0.0.1"),
        (SOCKS_ATYP_IPV6, "::1"),
        (SOCKS_ATYP_DOMAIN, "127.0.0.1"),
        (SOCKS_ATYP_DOMAIN, "::1"),
        (SOCKS_ATYP_DOMAIN, "evil.test"),
        (SOCKS_ATYP_DOMAIN, "evil6.test"),
    ],
    ids=["ipv4", "ipv6", "ipv4-as-name", "ipv6-as-name", "name-to-loopback", "name-to-ipv6"],
)
async def test_the_proxy_refuses_internal_targets_before_connecting(
    echo_server: EchoServer, address_type: int, host: str
) -> None:
    resolver = FakeResolver({"evil.test": ["127.0.0.1"], "evil6.test": ["fd12:3456::1"]})

    async with running(EgressGuard(resolver=resolver)) as proxy:
        code, _reader, writer = await socks_connect(proxy, address_type, host, echo_server.port)
        writer.close()

    assert code == SOCKS_REPLY_NOT_ALLOWED
    assert echo_server.connections == 0


async def test_the_proxy_reports_an_unresolvable_name_as_unreachable() -> None:
    async with running(EgressGuard(resolver=FakeResolver({}))) as proxy:
        code, _reader, writer = await socks_connect(proxy, SOCKS_ATYP_DOMAIN, "missing.test", 80)
        writer.close()

    assert code == SOCKS_REPLY_HOST_UNREACHABLE


async def test_the_proxy_reports_a_refused_connection(closed_port: int) -> None:
    guard = EgressGuard(exempt={(LOOPBACK, closed_port)})

    async with running(guard) as proxy:
        code, _reader, writer = await socks_connect(
            proxy, SOCKS_ATYP_IPV4, "127.0.0.1", closed_port
        )
        writer.close()

    assert code == SOCKS_REPLY_CONNECTION_REFUSED


async def test_the_proxy_supports_only_connect(echo_server: EchoServer) -> None:
    guard = EgressGuard(exempt={(LOOPBACK, echo_server.port)})

    async with running(guard) as proxy:
        code, _reader, writer = await socks_connect(
            proxy, SOCKS_ATYP_IPV4, "127.0.0.1", echo_server.port, command=SOCKS_BIND
        )
        writer.close()

    assert code == SOCKS_REPLY_COMMAND_NOT_SUPPORTED
    assert echo_server.connections == 0


async def test_the_proxy_refuses_clients_that_cannot_skip_authentication() -> None:
    async with running(EgressGuard()) as proxy:
        reader, writer = await _open(proxy)
        writer.write(bytes([5, 1, 2]))
        await writer.drain()
        reply = await reader.readexactly(2)
        writer.close()

    assert reply == bytes([5, SOCKS_NO_ACCEPTABLE_METHODS])


class FakeBrowser:
    version = "fake"

    def __init__(self) -> None:
        self.closed = False

    def is_connected(self) -> bool:
        return not self.closed

    async def close(self) -> None:
        self.closed = True

    async def new_context(self) -> object:
        return object()


class FakeChromium:
    def __init__(self) -> None:
        self.launches: list[dict[str, Any]] = []

    async def launch(self, **kwargs: Any) -> FakeBrowser:
        self.launches.append(kwargs)
        return FakeBrowser()


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()

    async def stop(self) -> None:
        return None


def _fake_playwright(
    monkeypatch: pytest.MonkeyPatch, backend: PlaywrightBrowserBackend
) -> FakePlaywright:
    playwright = FakePlaywright()

    async def start() -> FakePlaywright:
        return playwright

    monkeypatch.setattr(backend, "_start_playwright", start)
    return playwright


async def test_every_browser_launch_is_routed_through_the_egress_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PROXIED_LOOPBACK_OVERRIDE_ENV, raising=False)
    backend = PlaywrightBrowserBackend(launch_args=())
    playwright = _fake_playwright(monkeypatch, backend)

    await backend.new_context()
    (launch,) = playwright.chromium.launches
    proxy_url = launch["proxy"]["server"]
    host, port = proxy_url.removeprefix("socks5://").rsplit(":", 1)
    _reader, writer = await asyncio.open_connection(host, int(port))
    writer.close()

    assert proxy_url.startswith("socks5://127.0.0.1:")
    assert set(launch["proxy"]) == {"server"}
    assert all(arg in launch["args"] for arg in EGRESS_LAUNCH_ARGS)

    await backend.aclose()
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection(host, int(port))


async def test_the_browser_does_not_start_if_loopback_would_bypass_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROXIED_LOOPBACK_OVERRIDE_ENV, "1")
    backend = PlaywrightBrowserBackend()
    playwright = _fake_playwright(monkeypatch, backend)

    with pytest.raises(ConfigurationError, match=PROXIED_LOOPBACK_OVERRIDE_ENV):
        await backend.new_context()

    assert playwright.chromium.launches == []
    await backend.aclose()
