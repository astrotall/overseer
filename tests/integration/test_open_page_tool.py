from __future__ import annotations

import os
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

import pytest

from libs.browser import browser_context, init_browser_manager, reset_browser_manager
from libs.browser.session import BrowserSessionManager
from libs.core.config import Settings
from libs.tools import OpenPageTool
from libs.tools.open_page import NOT_HTML_TEXT, PAGE_UNAVAILABLE_TEXT

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

pytestmark = pytest.mark.browser

KNOWN_TITLE = "Example Domain"
KNOWN_PARAGRAPH_1 = "This is a page used for illustrative examples in documents."
KNOWN_PARAGRAPH_2 = "You may use this page without needing permission."


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


PAGES: dict[str, tuple[int, str, str]] = {
    "/known": (200, "text/html; charset=utf-8", _known_page()),
    "/empty": (200, "text/html; charset=utf-8", "<html><head></head><body></body></html>"),
    "/not-found": (404, "text/html; charset=utf-8", "<html><body>Not found</body></html>"),
    "/broken": (500, "text/html; charset=utf-8", "<html><body>Broken</body></html>"),
    "/data.json": (200, "application/json", '{"not": "html"}'),
}


class FakePageServer:
    def __init__(self) -> None:
        self.requests: list[SeenRequest] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def seen(self, path: str) -> list[SeenRequest]:
        with self._lock:
            return [request for request in self.requests if request.path == path]

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                visitor = cookies["visitor"].value if "visitor" in cookies else None
                issued = None if visitor else uuid.uuid4().hex

                with fake._lock:
                    fake.requests.append(SeenRequest(self.path, visitor, issued))

                status, content_type, body = PAGES[self.path]
                payload = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                if issued:
                    self.send_header("Set-Cookie", f"visitor={issued}; Path=/")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


@pytest.fixture
def page_server() -> Iterator[FakePageServer]:
    server = FakePageServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
async def browser_manager(
    settings: Settings,
) -> AsyncIterator[BrowserSessionManager[BrowserContext]]:
    reset_browser_manager()
    manager = init_browser_manager(settings)
    try:
        async with manager.acquire(uuid.uuid4()):
            pass
    except Exception as exc:
        await manager.aclose()
        reset_browser_manager()
        if os.getenv("CI"):
            raise
        pytest.skip(f"Chromium недоступен: {exc}")

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
async def test_an_unreachable_url_is_an_error_not_a_crash() -> None:
    with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as server:
        host, port = server.server_address[:2]
    closed_port_url = f"http://{host!s}:{port}/known"

    result = await OpenPageTool().execute({"url": closed_port_url}, conversation_id=uuid.uuid4())

    assert result.is_error
    assert result.error == PAGE_UNAVAILABLE_TEXT
