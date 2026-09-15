from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import Final
from urllib.parse import urlsplit

from libs.core.logging import get_logger

logger = get_logger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Endpoint = tuple[IPAddress, int]
Resolver = Callable[[str, int], Awaitable[Sequence[IPAddress]]]

DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443, "ws": 80, "wss": 443}

NAT64_WELL_KNOWN_PREFIX: Final = ipaddress.IPv6Network("64:ff9b::/96")
IPV4_COMPATIBLE_PREFIX: Final = ipaddress.IPv6Network("::/96")
IPV6_SITE_LOCAL: Final = ipaddress.IPv6Network("fec0::/10")
TEREDO_PREFIX: Final = ipaddress.IPv6Network("2001::/32")

PROXY_LISTEN_HOST: Final = "127.0.0.1"
NETWORK_TIMEOUT_S: Final = 10.0
PIPE_CHUNK_BYTES: Final = 64 * 1024

SOCKS_VERSION: Final = 5
SOCKS_NO_AUTH: Final = 0
SOCKS_NO_ACCEPTABLE_METHODS: Final = 0xFF
SOCKS_CMD_CONNECT: Final = 1
SOCKS_ATYP_IPV4: Final = 1
SOCKS_ATYP_DOMAIN: Final = 3
SOCKS_ATYP_IPV6: Final = 4
SOCKS_REPLY_SUCCEEDED: Final = 0
SOCKS_REPLY_GENERAL_FAILURE: Final = 1
SOCKS_REPLY_NOT_ALLOWED: Final = 2
SOCKS_REPLY_HOST_UNREACHABLE: Final = 4
SOCKS_REPLY_CONNECTION_REFUSED: Final = 5
SOCKS_REPLY_COMMAND_NOT_SUPPORTED: Final = 7
SOCKS_REPLY_ADDRESS_TYPE_NOT_SUPPORTED: Final = 8


class EgressDeniedError(Exception):
    def __init__(self, address: IPAddress, port: int) -> None:
        super().__init__(f"Адрес {address} не публичный: подключение к нему запрещено")
        self.address = address
        self.port = port


def is_public_address(address: IPAddress) -> bool:
    embedded = _embedded_ipv4(address)
    if embedded is not None:
        return is_public_address(embedded)
    if isinstance(address, ipaddress.IPv6Address) and (
        address in IPV6_SITE_LOCAL or address in TEREDO_PREFIX
    ):
        return False
    return address.is_global and not (address.is_multicast or address.is_reserved)


def _embedded_ipv4(address: IPAddress) -> ipaddress.IPv4Address | None:
    if isinstance(address, ipaddress.IPv4Address):
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address in NAT64_WELL_KNOWN_PREFIX or address in IPV4_COMPATIBLE_PREFIX:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


async def system_resolver(host: str, port: int) -> list[IPAddress]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(info[4][0]) for info in infos]


class EgressGuard:
    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        exempt: Collection[Endpoint] = (),
    ) -> None:
        self._resolver = resolver
        self._exempt = frozenset(exempt)

    async def resolve(self, host: str, port: int) -> list[IPAddress]:
        literal = _ip_literal(host)
        if literal is not None:
            addresses = [literal]
        else:
            answers = await asyncio.wait_for(self._resolver(host, port), NETWORK_TIMEOUT_S)
            addresses = list(dict.fromkeys(answers))
        if not addresses:
            raise OSError("имя не разрешилось ни в один адрес")

        for address in addresses:
            if (address, port) not in self._exempt and not is_public_address(address):
                raise EgressDeniedError(address, port)
        return addresses

    async def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or DEFAULT_PORTS.get(parts.scheme)
        if not host or port is None:
            raise ValueError("в адресе нет хоста или порта, который можно проверить")
        await self.resolve(host, port)


def _ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


class EgressProxy:
    def __init__(self, guard: EgressGuard) -> None:
        self._guard = guard
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.StreamWriter] = set()

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def server_url(self) -> str:
        if self._server is None:
            raise RuntimeError("Прокси исходящего трафика браузера не запущен")
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"socks5://{host}:{port}"

    async def start(self) -> None:
        if self._server is None:
            self._server = await asyncio.start_server(self._handle, PROXY_LISTEN_HOST, 0)

    async def aclose(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        for client in list(self._clients):
            client.close()
        await server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        try:
            target = await asyncio.wait_for(
                _read_connect_request(reader, writer), NETWORK_TIMEOUT_S
            )
            if target is None:
                return
            upstream = await self._open_upstream(writer, *target)
            if upstream is None:
                return
            upstream_reader, upstream_writer = upstream
            try:
                await _reply(writer, SOCKS_REPLY_SUCCEEDED)
                await _relay(reader, writer, upstream_reader, upstream_writer)
            finally:
                upstream_writer.close()
        except (OSError, asyncio.IncompleteReadError):
            return
        except Exception:
            logger.exception("browser.egress_proxy_failed")
        finally:
            self._clients.discard(writer)
            writer.close()

    async def _open_upstream(
        self, writer: asyncio.StreamWriter, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        try:
            addresses = await self._guard.resolve(host, port)
        except EgressDeniedError as exc:
            logger.warning("browser.egress_denied", address=str(exc.address), port=exc.port)
            await _reply(writer, SOCKS_REPLY_NOT_ALLOWED)
            return None
        except OSError:
            await _reply(writer, SOCKS_REPLY_HOST_UNREACHABLE)
            return None

        for address in addresses:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(str(address), port), NETWORK_TIMEOUT_S
                )
            except OSError:
                continue
        await _reply(writer, SOCKS_REPLY_CONNECTION_REFUSED)
        return None


async def _read_connect_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> tuple[str, int] | None:
    version, method_count = await reader.readexactly(2)
    if version != SOCKS_VERSION:
        return None
    methods = await reader.readexactly(method_count)
    if SOCKS_NO_AUTH not in methods:
        writer.write(bytes([SOCKS_VERSION, SOCKS_NO_ACCEPTABLE_METHODS]))
        await writer.drain()
        return None
    writer.write(bytes([SOCKS_VERSION, SOCKS_NO_AUTH]))
    await writer.drain()

    version, command, _reserved, address_type = await reader.readexactly(4)
    if version != SOCKS_VERSION:
        return None
    if address_type == SOCKS_ATYP_IPV4:
        host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
    elif address_type == SOCKS_ATYP_IPV6:
        host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
    elif address_type == SOCKS_ATYP_DOMAIN:
        length = (await reader.readexactly(1))[0]
        try:
            host = (await reader.readexactly(length)).decode("ascii")
        except UnicodeDecodeError:
            await _reply(writer, SOCKS_REPLY_GENERAL_FAILURE)
            return None
    else:
        await _reply(writer, SOCKS_REPLY_ADDRESS_TYPE_NOT_SUPPORTED)
        return None
    port = int.from_bytes(await reader.readexactly(2), "big")

    if command != SOCKS_CMD_CONNECT:
        await _reply(writer, SOCKS_REPLY_COMMAND_NOT_SUPPORTED)
        return None
    return host, port


async def _reply(writer: asyncio.StreamWriter, code: int) -> None:
    writer.write(bytes([SOCKS_VERSION, code, 0, SOCKS_ATYP_IPV4, 0, 0, 0, 0, 0, 0]))
    await writer.drain()


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    pipes = {
        asyncio.ensure_future(_pipe(client_reader, upstream_writer)),
        asyncio.ensure_future(_pipe(upstream_reader, client_writer)),
    }
    try:
        await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for pipe in pipes:
            pipe.cancel()
        await asyncio.gather(*pipes, return_exceptions=True)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(PIPE_CHUNK_BYTES):
            writer.write(chunk)
            await writer.drain()
    except OSError:
        return
