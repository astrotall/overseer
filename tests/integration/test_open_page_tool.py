from __future__ import annotations

import asyncio
import html
import ipaddress
import os
import socket
import threading
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import pytest

from libs.browser import (
    EgressGuard,
    browser_context,
    init_browser_manager,
    reset_browser_manager,
)
from libs.browser.egress import IPAddress
from libs.browser.session import BrowserSessionManager
from libs.core.config import Settings
from libs.tools import OpenPageTool
from libs.tools import open_page as open_page_module
from libs.tools.open_page import (
    BLOCKED_ADDRESS_TEXT,
    EXTRACT_LIMITS,
    EXTRACT_PAGE_JS,
    MAX_CONTENT_CHARS,
    MAX_PARAGRAPHS,
    MAX_TITLE_CHARS,
    NOT_HTML_TEXT,
    PAGE_CHANGED_TEXT,
    PAGE_TIMEOUT_SUMMARY,
    PAGE_UNAVAILABLE_TEXT,
    REFRESH_CHAIN_TEXT,
    REFRESH_TIMEOUT_SUMMARY,
    build_page_summary,
    extract_page,
)

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

pytestmark = pytest.mark.browser

LOOPBACK = ipaddress.IPv4Address("127.0.0.1")

SHORT_NAVIGATION_TIMEOUT_MS = 500
SHORT_REFRESH_TIMEOUT_MS = 500
REFRESH_REPEATS = 20
STUB_TITLE = "Перенаправление"
STUB_PARAGRAPH = "Сейчас вы будете перенаправлены на нужную страницу."
TIMEOUT_CALL_DEADLINE_S = 20.0

KNOWN_TITLE = "Example Domain"
KNOWN_PARAGRAPH_1 = "This is a page used for illustrative examples in documents."
KNOWN_PARAGRAPH_2 = "You may use this page without needing permission."

REACH_INTERNAL_JS = """
async (url) => {
  const fetched = await fetch(url, { mode: "no-cors" }).then(() => "reached", () => "refused");
  const socket = await new Promise((resolve) => {
    const ws = new WebSocket(url.replace("http", "ws"));
    ws.onopen = () => resolve("reached");
    ws.onerror = () => resolve("refused");
  });
  const image = await new Promise((resolve) => {
    const img = document.createElement("img");
    img.onload = () => resolve("reached");
    img.onerror = () => resolve("refused");
    img.src = url;
    document.body.appendChild(img);
  });
  return { fetched, socket, image };
}
"""


@dataclass(frozen=True, slots=True)
class SeenRequest:
    path: str
    visitor: str | None
    issued: str | None


def _known_page() -> str:
    return (
        f"<!DOCTYPE html><html><head><title>{KNOWN_TITLE}</title></head>"
        f"<body><main><h1>{KNOWN_TITLE}</h1>"
        f"<p>{KNOWN_PARAGRAPH_1}</p>"
        f"<p>{KNOWN_PARAGRAPH_2}</p>"
        "</main></body></html>"
    )


def _refresh_stub(content: str) -> str:
    return (
        f"<!DOCTYPE html><html><head><title>{STUB_TITLE}</title>"
        f'<meta http-equiv="refresh" content="{html.escape(content)}"></head>'
        f"<body><main><p>{STUB_PARAGRAPH}</p></main></body></html>"
    )


PAGES: dict[str, tuple[int, str, str]] = {
    "/known": (200, "text/html; charset=utf-8", _known_page()),
    "/empty": (200, "text/html; charset=utf-8", "<html><head></head><body></body></html>"),
    "/not-found": (404, "text/html; charset=utf-8", "<html><body>Not found</body></html>"),
    "/broken": (500, "text/html; charset=utf-8", "<html><body>Broken</body></html>"),
    "/data.json": (200, "application/json", '{"not": "html"}'),
    "/report.pdf": (200, "application/pdf", "%PDF-1.4 not a page"),
    "/attachment": (200, "text/html; charset=utf-8", "<html><body>saved, not shown</body></html>"),
    "/refresh-header": (
        200,
        "text/html; charset=utf-8",
        f"<html><head><title>{STUB_TITLE}</title></head><body><p>{STUB_PARAGRAPH}</p></body></html>",
    ),
    "/self-refresh": (200, "text/html; charset=utf-8", _refresh_stub("2; url=/self-refresh")),
}

EXTRA_HEADERS: dict[str, tuple[tuple[str, str], ...]] = {
    "/attachment": (("Content-Disposition", 'attachment; filename="page.html"'),),
    "/refresh-header": (("Refresh", "0; url=/known"),),
}


STALLED_PATHS = frozenset({"/hang", "/stall"})
STALL_LIMIT_S = 30.0


class _IPv6HTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class FakePageServer:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self.requests: list[SeenRequest] = []
        self.release = threading.Event()
        self._host = host
        self._lock = threading.Lock()
        server_class = _IPv6HTTPServer if ":" in host else ThreadingHTTPServer
        self._server = server_class((host, 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        host = f"[{self._host}]" if ":" in self._host else self._host
        return f"http://{host}:{self.port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def redirect_url(self, target: str) -> str:
        return self.url(f"/redirect?to={quote(target, safe='')}")

    def refresh_url(self, content: str) -> str:
        return self.url(f"/refresh?{urlencode({'content': content})}")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()

    def seen(self, path: str) -> list[SeenRequest]:
        with self._lock:
            return [request for request in self.requests if request.path == path]

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                visitor = cookies["visitor"].value if "visitor" in cookies else None
                issued = None if visitor else uuid.uuid4().hex

                with fake._lock:
                    fake.requests.append(SeenRequest(parts.path, visitor, issued))

                if parts.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", parse_qs(parts.query)["to"][0])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if parts.path == "/refresh":
                    payload = _refresh_stub(parse_qs(parts.query)["content"][0]).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                if parts.path in STALLED_PATHS:
                    if parts.path == "/stall":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", "1000")
                        self.end_headers()
                        self.wfile.write(b"<html><body>")
                        self.wfile.flush()
                    fake.release.wait(STALL_LIMIT_S)
                    return

                status, content_type, body = PAGES.get(
                    parts.path, (404, "text/html; charset=utf-8", "<html></html>")
                )
                payload = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                for name, value in EXTRA_HEADERS.get(parts.path, ()):
                    self.send_header(name, value)
                if issued:
                    self.send_header("Set-Cookie", f"visitor={issued}; Path=/")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


class FakeResolver:
    def __init__(self) -> None:
        self._answers: dict[str, list[Sequence[str]]] = {}

    def answer(self, host: str, *answers: Sequence[str]) -> None:
        self._answers[host] = list(answers)

    async def __call__(self, host: str, port: int) -> list[IPAddress]:
        queue = self._answers.get(host)
        if not queue:
            raise OSError(f"неизвестное имя {host}")
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        return [ipaddress.ip_address(address) for address in answer]


@pytest.fixture
def page_server() -> Iterator[FakePageServer]:
    server = FakePageServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def internal_server() -> Iterator[FakePageServer]:
    server = FakePageServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def internal_ipv6_server() -> Iterator[FakePageServer]:
    try:
        server = FakePageServer("::1")
    except OSError as exc:
        pytest.skip(f"IPv6 loopback недоступен: {exc}")
    server.start()
    yield server
    server.stop()


@pytest.fixture
def closed_port() -> int:
    with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as server:
        return int(server.server_address[1])


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver()


async def _started_manager(
    settings: Settings, guard: EgressGuard | None
) -> BrowserSessionManager[BrowserContext]:
    reset_browser_manager()
    manager = init_browser_manager(settings, egress_guard=guard)
    try:
        async with manager.acquire(uuid.uuid4()):
            pass
    except Exception as exc:
        await manager.aclose()
        reset_browser_manager()
        if os.getenv("CI"):
            raise
        pytest.skip(f"Chromium недоступен: {exc}")
    return manager


@pytest.fixture
async def browser_manager(
    settings: Settings, page_server: FakePageServer, closed_port: int, resolver: FakeResolver
) -> AsyncIterator[BrowserSessionManager[BrowserContext]]:
    guard = EgressGuard(
        resolver=resolver, exempt={(LOOPBACK, page_server.port), (LOOPBACK, closed_port)}
    )
    manager = await _started_manager(settings, guard)
    yield manager
    await manager.aclose()
    reset_browser_manager()


@pytest.fixture
async def public_browser_manager(
    settings: Settings,
) -> AsyncIterator[BrowserSessionManager[BrowserContext]]:
    manager = await _started_manager(settings, None)
    yield manager
    await manager.aclose()
    reset_browser_manager()


@pytest.mark.usefixtures("browser_manager")
async def test_a_known_page_is_extracted_as_expected(page_server: FakePageServer) -> None:
    result = await OpenPageTool().execute(
        {"url": page_server.url("/known")}, conversation_id=uuid.uuid4()
    )

    assert not result.is_error, result.error
    assert result.data["title"] == KNOWN_TITLE
    assert result.data["content"] == f"{KNOWN_PARAGRAPH_1}\n\n{KNOWN_PARAGRAPH_2}"
    assert result.data["url"] == page_server.url("/known")


@pytest.mark.usefixtures("browser_manager")
async def test_an_empty_page_is_a_success_with_no_content(page_server: FakePageServer) -> None:
    result = await OpenPageTool().execute(
        {"url": page_server.url("/empty")}, conversation_id=uuid.uuid4()
    )

    assert not result.is_error, result.error
    assert result.data == {"title": "", "url": page_server.url("/empty"), "content": ""}


@pytest.mark.usefixtures("browser_manager")
async def test_the_page_is_closed_after_the_call(page_server: FakePageServer) -> None:
    conversation_id = uuid.uuid4()

    await OpenPageTool().execute(
        {"url": page_server.url("/known")}, conversation_id=conversation_id
    )

    async with browser_context(conversation_id) as context:
        assert context.pages == []


async def test_one_conversation_reuses_its_browser_session_across_calls(
    browser_manager: BrowserSessionManager[BrowserContext], page_server: FakePageServer
) -> None:
    tool = OpenPageTool()
    conversation_id = uuid.uuid4()
    sessions_before = browser_manager.active_sessions

    await tool.execute({"url": page_server.url("/known")}, conversation_id=conversation_id)
    await tool.execute({"url": page_server.url("/known")}, conversation_id=conversation_id)

    first, second = page_server.seen("/known")
    assert first.visitor is None
    assert first.issued is not None
    assert second.visitor == first.issued
    assert browser_manager.active_sessions == sessions_before + 1


async def test_different_conversations_open_pages_in_isolated_browser_sessions(
    browser_manager: BrowserSessionManager[BrowserContext], page_server: FakePageServer
) -> None:
    tool = OpenPageTool()
    sessions_before = browser_manager.active_sessions

    await tool.execute({"url": page_server.url("/known")}, conversation_id=uuid.uuid4())
    await tool.execute({"url": page_server.url("/known")}, conversation_id=uuid.uuid4())

    first, second = page_server.seen("/known")
    assert first.visitor is None
    assert second.visitor is None
    assert second.issued != first.issued
    assert browser_manager.active_sessions == sessions_before + 2


@pytest.mark.usefixtures("browser_manager")
async def test_an_http_error_is_reported_with_its_status(page_server: FakePageServer) -> None:
    result = await OpenPageTool().execute(
        {"url": page_server.url("/not-found")}, conversation_id=uuid.uuid4()
    )

    assert result.is_error
    assert result.error is not None
    assert "404" in result.error


@pytest.mark.usefixtures("browser_manager")
async def test_a_server_error_is_reported_with_its_status(page_server: FakePageServer) -> None:
    result = await OpenPageTool().execute(
        {"url": page_server.url("/broken")}, conversation_id=uuid.uuid4()
    )

    assert result.is_error
    assert result.error is not None
    assert "500" in result.error


@pytest.mark.usefixtures("browser_manager")
async def test_non_html_content_is_a_clear_failure_not_a_crash(page_server: FakePageServer) -> None:
    result = await OpenPageTool().execute(
        {"url": page_server.url("/data.json")}, conversation_id=uuid.uuid4()
    )

    assert result.is_error
    assert result.error == NOT_HTML_TEXT


@pytest.mark.usefixtures("browser_manager")
@pytest.mark.parametrize("path", ["/report.pdf", "/attachment"])
async def test_a_file_the_browser_would_download_is_reported_as_not_html(
    page_server: FakePageServer, path: str
) -> None:
    result = await OpenPageTool().execute(
        {"url": page_server.url(path)}, conversation_id=uuid.uuid4()
    )

    assert result.is_error
    assert result.error == NOT_HTML_TEXT


@pytest.mark.usefixtures("browser_manager")
@pytest.mark.parametrize("path", ["/hang", "/stall"])
async def test_a_page_that_never_finishes_loading_times_out_with_a_clear_error_naming_the_url(
    monkeypatch: pytest.MonkeyPatch, page_server: FakePageServer, path: str
) -> None:
    monkeypatch.setattr(open_page_module, "NAVIGATION_TIMEOUT_MS", SHORT_NAVIGATION_TIMEOUT_MS)
    url = page_server.url(path)
    conversation_id = uuid.uuid4()

    async with asyncio.timeout(TIMEOUT_CALL_DEADLINE_S):
        result = await OpenPageTool().execute({"url": url}, conversation_id=conversation_id)

    assert result.is_error
    assert result.error is not None
    assert url in result.error
    assert "0.5 с" in result.error
    assert result.error != PAGE_UNAVAILABLE_TEXT
    assert result.summary == PAGE_TIMEOUT_SUMMARY
    assert url not in result.summary
    async with browser_context(conversation_id) as context:
        assert context.pages == []


@pytest.mark.usefixtures("browser_manager")
async def test_the_url_in_a_timeout_error_is_shortened(
    monkeypatch: pytest.MonkeyPatch, page_server: FakePageServer
) -> None:
    monkeypatch.setattr(open_page_module, "NAVIGATION_TIMEOUT_MS", SHORT_NAVIGATION_TIMEOUT_MS)
    url = page_server.url("/hang") + "?" + "x" * 1500

    async with asyncio.timeout(TIMEOUT_CALL_DEADLINE_S):
        result = await OpenPageTool().execute({"url": url}, conversation_id=uuid.uuid4())

    assert result.error is not None
    assert len(result.error) < 500
    assert page_server.url("/hang") in result.error


@pytest.mark.usefixtures("browser_manager")
async def test_a_page_that_navigates_away_while_it_is_read_is_a_clear_failure(
    monkeypatch: pytest.MonkeyPatch, page_server: FakePageServer
) -> None:
    from playwright.async_api import Error as PlaywrightError

    async def destroyed(page: object) -> dict[str, object]:
        raise PlaywrightError("Page.evaluate: Execution context was destroyed")

    monkeypatch.setattr(open_page_module, "extract_page", destroyed)

    result = await OpenPageTool().execute(
        {"url": page_server.url("/known")}, conversation_id=uuid.uuid4()
    )

    assert result.is_error
    assert result.error == PAGE_CHANGED_TEXT


@pytest.mark.usefixtures("browser_manager")
@pytest.mark.parametrize(
    "content",
    ["0; url=/known", "0;URL='/known'", "0, /known", "1; url=/known"],
    ids=["immediate", "quoted", "without-url-prefix", "one-second"],
)
async def test_a_meta_refresh_stub_is_followed_to_the_real_page_every_time(
    page_server: FakePageServer, content: str
) -> None:
    tool = OpenPageTool()

    for _ in range(REFRESH_REPEATS if content.startswith("0") else 2):
        result = await tool.execute(
            {"url": page_server.refresh_url(content)}, conversation_id=uuid.uuid4()
        )

        assert not result.is_error, result.error
        assert result.data == {
            "title": KNOWN_TITLE,
            "url": page_server.url("/known"),
            "content": f"{KNOWN_PARAGRAPH_1}\n\n{KNOWN_PARAGRAPH_2}",
        }


@pytest.mark.usefixtures("browser_manager")
async def test_a_refresh_header_stub_is_followed_to_the_real_page(
    page_server: FakePageServer,
) -> None:
    for _ in range(REFRESH_REPEATS):
        result = await OpenPageTool().execute(
            {"url": page_server.url("/refresh-header")}, conversation_id=uuid.uuid4()
        )

        assert not result.is_error, result.error
        assert result.data["url"] == page_server.url("/known")
        assert result.data["content"] == f"{KNOWN_PARAGRAPH_1}\n\n{KNOWN_PARAGRAPH_2}"


@pytest.mark.usefixtures("browser_manager")
@pytest.mark.parametrize(
    "path", ["/refresh?content=3", "/self-refresh"], ids=["no-url", "same-url"]
)
async def test_a_page_that_only_refreshes_itself_is_returned_without_waiting(
    page_server: FakePageServer, path: str
) -> None:
    url = page_server.url(path)

    started = asyncio.get_running_loop().time()
    result = await OpenPageTool().execute({"url": url}, conversation_id=uuid.uuid4())

    assert asyncio.get_running_loop().time() - started < 2
    assert not result.is_error, result.error
    assert result.data == {"title": STUB_TITLE, "url": url, "content": STUB_PARAGRAPH}


@pytest.mark.usefixtures("browser_manager")
async def test_a_refresh_too_far_in_the_future_is_reported_instead_of_waited_for(
    page_server: FakePageServer,
) -> None:
    url = page_server.refresh_url("30; url=/known")

    result = await OpenPageTool().execute({"url": url}, conversation_id=uuid.uuid4())

    assert not result.is_error, result.error
    assert result.data["content"] == STUB_PARAGRAPH
    assert result.data["refresh_url"] == page_server.url("/known")
    assert page_server.seen("/known") == []


@pytest.mark.usefixtures("browser_manager")
async def test_a_refresh_to_a_missing_page_reports_the_target_status(
    page_server: FakePageServer,
) -> None:
    result = await OpenPageTool().execute(
        {"url": page_server.refresh_url("0; url=/not-found")}, conversation_id=uuid.uuid4()
    )

    assert result.is_error
    assert result.error is not None
    assert "404" in result.error


@pytest.mark.usefixtures("browser_manager")
async def test_a_refresh_to_a_file_is_reported_as_not_html(page_server: FakePageServer) -> None:
    result = await OpenPageTool().execute(
        {"url": page_server.refresh_url("0; url=/report.pdf")}, conversation_id=uuid.uuid4()
    )

    assert result.error == NOT_HTML_TEXT


@pytest.mark.usefixtures("browser_manager")
async def test_a_refresh_that_never_arrives_is_a_distinct_failure_not_the_stub(
    monkeypatch: pytest.MonkeyPatch, page_server: FakePageServer
) -> None:
    monkeypatch.setattr(open_page_module, "REFRESH_TIMEOUT_MS", SHORT_REFRESH_TIMEOUT_MS)

    async with asyncio.timeout(TIMEOUT_CALL_DEADLINE_S):
        result = await OpenPageTool().execute(
            {"url": page_server.refresh_url("0; url=/hang")}, conversation_id=uuid.uuid4()
        )

    assert result.is_error
    assert result.error is not None
    assert page_server.url("/hang") in result.error
    assert STUB_PARAGRAPH not in result.error
    assert result.summary == REFRESH_TIMEOUT_SUMMARY


@pytest.mark.usefixtures("browser_manager")
async def test_a_refresh_chain_longer_than_the_limit_is_cut(page_server: FakePageServer) -> None:
    url = page_server.url("/known")
    for _ in range(open_page_module.MAX_REFRESH_HOPS + 1):
        url = page_server.refresh_url(f"1; url={url}")

    result = await OpenPageTool().execute({"url": url}, conversation_id=uuid.uuid4())

    assert result.error == REFRESH_CHAIN_TEXT
    assert page_server.seen("/known") == []


@pytest.mark.usefixtures("browser_manager")
@pytest.mark.parametrize("answers", [[["127.0.0.2"]], [["127.0.0.1"], ["127.0.0.2"]]])
async def test_a_refresh_into_the_internal_network_is_refused(
    page_server: FakePageServer, resolver: FakeResolver, answers: list[list[str]]
) -> None:
    resolver.answer("rebind.test", *answers)
    target = f"http://rebind.test:{page_server.port}/known"

    result = await OpenPageTool().execute(
        {"url": page_server.refresh_url(f"1; url={target}")}, conversation_id=uuid.uuid4()
    )

    assert result.error == BLOCKED_ADDRESS_TEXT
    assert page_server.seen("/known") == []


@pytest.mark.usefixtures("browser_manager")
async def test_an_unreachable_url_is_an_error_not_a_crash(closed_port: int) -> None:
    result = await OpenPageTool().execute(
        {"url": f"http://127.0.0.1:{closed_port}/known"}, conversation_id=uuid.uuid4()
    )

    assert result.is_error
    assert result.error == PAGE_UNAVAILABLE_TEXT


@pytest.mark.usefixtures("browser_manager")
async def test_a_loopback_address_is_refused_and_never_contacted(
    internal_server: FakePageServer,
) -> None:
    result = await OpenPageTool().execute(
        {"url": internal_server.url("/known")}, conversation_id=uuid.uuid4()
    )

    assert result.error == BLOCKED_ADDRESS_TEXT
    assert internal_server.requests == []


@pytest.mark.usefixtures("browser_manager")
async def test_ipv6_loopback_is_refused_and_never_contacted(
    internal_ipv6_server: FakePageServer,
) -> None:
    result = await OpenPageTool().execute(
        {"url": internal_ipv6_server.url("/known")}, conversation_id=uuid.uuid4()
    )

    assert result.error == BLOCKED_ADDRESS_TEXT
    assert internal_ipv6_server.requests == []


@pytest.mark.usefixtures("browser_manager")
@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/",
        "http://172.17.0.1/",
        "http://192.168.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://[::]/",
        "http://[fd12:3456::1]/",
        "http://[fc00::1]:5432/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:172.18.0.2]:6379/",
    ],
)
async def test_private_link_local_and_mapped_addresses_are_refused(url: str) -> None:
    result = await OpenPageTool().execute({"url": url}, conversation_id=uuid.uuid4())

    assert result.error == BLOCKED_ADDRESS_TEXT


@pytest.mark.usefixtures("browser_manager")
@pytest.mark.parametrize(
    "answers",
    [["127.0.0.1"], ["::1"], ["fd12:3456::1"], ["172.18.0.2"], ["93.184.215.14", "127.0.0.1"]],
    ids=["loopback", "ipv6-loopback", "ipv6-unique-local", "docker-network", "mixed-answer"],
)
async def test_a_domain_that_resolves_into_the_internal_network_is_refused(
    internal_server: FakePageServer, resolver: FakeResolver, answers: list[str]
) -> None:
    resolver.answer("evil.test", answers)

    result = await OpenPageTool().execute(
        {"url": f"http://evil.test:{internal_server.port}/known"}, conversation_id=uuid.uuid4()
    )

    assert result.error == BLOCKED_ADDRESS_TEXT
    assert internal_server.requests == []


@pytest.mark.usefixtures("browser_manager")
@pytest.mark.parametrize("target", ["ip", "domain", "ipv6"])
async def test_a_redirect_into_the_internal_network_is_refused_at_the_redirect(
    page_server: FakePageServer,
    internal_server: FakePageServer,
    resolver: FakeResolver,
    target: str,
) -> None:
    resolver.answer("evil.test", ["127.0.0.1"])
    internal_urls = {
        "ip": internal_server.url("/known"),
        "domain": f"http://evil.test:{internal_server.port}/known",
        "ipv6": f"http://[::1]:{internal_server.port}/known",
    }

    result = await OpenPageTool().execute(
        {"url": page_server.redirect_url(internal_urls[target])}, conversation_id=uuid.uuid4()
    )

    assert result.error == BLOCKED_ADDRESS_TEXT
    assert len(page_server.seen("/redirect")) == 1
    assert internal_server.requests == []


@pytest.mark.usefixtures("browser_manager")
async def test_dns_rebinding_between_the_check_and_the_connection_is_refused(
    page_server: FakePageServer, resolver: FakeResolver
) -> None:
    resolver.answer("rebind.test", ["127.0.0.1"], ["127.0.0.2"])

    result = await OpenPageTool().execute(
        {"url": f"http://rebind.test:{page_server.port}/known"}, conversation_id=uuid.uuid4()
    )

    assert result.error == BLOCKED_ADDRESS_TEXT
    assert page_server.requests == []


@pytest.mark.usefixtures("browser_manager")
async def test_the_browser_itself_cannot_reach_the_internal_network(
    page_server: FakePageServer, internal_server: FakePageServer, resolver: FakeResolver
) -> None:
    from playwright.async_api import Error as PlaywrightError

    resolver.answer("evil.test", ["127.0.0.1"])

    async with browser_context(uuid.uuid4()) as context:
        for url in (
            internal_server.url("/known"),
            f"http://evil.test:{internal_server.port}/known",
        ):
            page = await context.new_page()
            with pytest.raises(PlaywrightError):
                await page.goto(url)
            await page.close()

        page = await context.new_page()
        await page.goto(page_server.url("/known"))
        outcome = await page.evaluate(REACH_INTERNAL_JS, internal_server.url("/known"))
        await page.close()

    assert outcome == {"fetched": "refused", "socket": "refused", "image": "refused"}
    assert internal_server.requests == []


@pytest.mark.usefixtures("public_browser_manager")
@pytest.mark.skipif(
    os.getenv("CI") is not None,
    reason=(
        "живая внешняя страница никогда не открывается в CI — раннер без sandbox, "
        "см. architecture-browser.md, раздел «Песочница Chromium»"
    ),
)
async def test_a_real_public_page_still_opens_through_the_egress_policy() -> None:
    result = await OpenPageTool().execute(
        {"url": "https://example.com/"}, conversation_id=uuid.uuid4()
    )

    if result.error == PAGE_UNAVAILABLE_TEXT:
        pytest.skip("example.com недоступен по сети")

    assert result.error != BLOCKED_ADDRESS_TEXT
    assert not result.is_error, result.error
    assert result.data["title"] == KNOWN_TITLE


@pytest.mark.usefixtures("browser_manager")
async def test_a_huge_page_is_clipped_inside_the_browser_before_it_crosses_ipc() -> None:
    huge_paragraph = "слово " * 50_000
    paragraphs = "".join(f"<p>{huge_paragraph}</p>" for _ in range(100))
    title = "🙂" * 5_000

    async with browser_context(uuid.uuid4()) as context:
        page = await context.new_page()
        await page.set_content(
            f"<html><head><title>{title}</title></head><body><main>"
            f"<p>   </p>{paragraphs}</main></body></html>"
        )
        with_paragraphs = await extract_page(page)
        await page.set_content(f"<html><body><div>{huge_paragraph}</div></body></html>")
        without_paragraphs = await page.evaluate(EXTRACT_PAGE_JS, EXTRACT_LIMITS)
        await page.close()

    assert len(with_paragraphs["paragraphs"]) == MAX_PARAGRAPHS
    assert all(len(text) == MAX_CONTENT_CHARS + 1 for text in with_paragraphs["paragraphs"])
    assert with_paragraphs["fallback"] == ""
    assert len(with_paragraphs["title"]) == MAX_TITLE_CHARS + 1
    assert without_paragraphs["paragraphs"] == []
    assert len(without_paragraphs["fallback"]) == MAX_CONTENT_CHARS + 1

    summary = build_page_summary(
        url="about:blank",
        title=with_paragraphs["title"],
        paragraphs=with_paragraphs["paragraphs"],
        fallback=with_paragraphs["fallback"],
    )
    assert len(summary.title) == MAX_TITLE_CHARS
    assert summary.title.endswith("…")
    assert len(summary.content) == MAX_CONTENT_CHARS
    assert summary.content.endswith("…")
