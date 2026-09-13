from __future__ import annotations

import os
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from libs.browser import browser_context, init_browser_manager, reset_browser_manager
from libs.browser.session import BrowserSessionManager
from libs.core.config import Settings
from libs.tools import WebSearchTool
from libs.tools.web_search import (
    MAX_RESULTS,
    SEARCH_BLOCKED_TEXT,
    SEARCH_UNAVAILABLE_TEXT,
    UNRECOGNIZED_PAGE_TEXT,
)

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

pytestmark = pytest.mark.browser

LIVE_QUERY = "python programming language"


@dataclass(frozen=True, slots=True)
class SeenRequest:
    query: str
    visitor: str | None
    issued: str | None


def _result(title: str, href: str, snippet: str, *, css_class: str = "") -> str:
    return (
        f'<div class="result results_links results_links_deep web-result {css_class}">'
        '<div class="links_main links_deep result__body">'
        f'<h2 class="result__title"><a rel="nofollow" class="result__a" href="{href}">{title}</a>'
        "</h2>"
        f'<a class="result__snippet" href="{href}">{snippet}</a>'
        "</div></div>"
    )


def _page(body: str) -> str:
    return f"<!DOCTYPE html><html><head><title>DuckDuckGo</title></head><body>{body}</body></html>"


def _results_page() -> str:
    organic = [
        _result(
            f"Результат <b>{index}</b>",
            f"//duckduckgo.com/l/?uddg={quote(f'https://site{index}.example/page', safe='')}&rut=x",
            f"Фрагмент\n   номер <b>{index}</b>",
        )
        for index in range(MAX_RESULTS + 3)
    ]
    ad = _result(
        "Реклама", "https://duckduckgo.com/y.js?ad_domain=shop.example", "", css_class="result--ad"
    )
    duplicate = _result("Дубликат", "https://site0.example/page", "тот же адрес")
    return _page(f'<div id="links">{ad}{organic[0]}{duplicate}{"".join(organic[1:])}</div>')


PAGES: dict[str, tuple[int, str]] = {
    "blocked": (
        202,
        _page(
            '<div class="anomaly-modal__modal"><p>Unfortunately, bots use DuckDuckGo too.</p>'
            '<form id="challenge-form" method="POST"></form></div>'
        ),
    ),
    "forbidden": (403, _page("<p>Forbidden</p>")),
    "server-error": (500, _page("<p>Internal error</p>")),
    "nothing": (
        200,
        _page(
            '<div class="result results_links results_links_deep result--no-result">'
            '<div class="no-results">No results.</div></div>'
        ),
    ),
    "redesigned": (200, _page('<main><article class="serp-item">Новая вёрстка</article></main>')),
}


class FakeDuckDuckGo:
    def __init__(self) -> None:
        self.requests: list[SeenRequest] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}/html/"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def seen(self, query: str) -> list[SeenRequest]:
        with self._lock:
            return [request for request in self.requests if request.query == query]

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                visitor = cookies["visitor"].value if "visitor" in cookies else None
                issued = None if visitor else uuid.uuid4().hex

                with fake._lock:
                    fake.requests.append(SeenRequest(query, visitor, issued))

                status, body = PAGES.get(query, (200, _results_page()))
                payload = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                if issued:
                    self.send_header("Set-Cookie", f"visitor={issued}; Path=/")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


@pytest.fixture
def fake_duckduckgo() -> Iterator[FakeDuckDuckGo]:
    fake = FakeDuckDuckGo()
    fake.start()
    yield fake
    fake.stop()


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


def _query() -> str:
    return f"запрос {uuid.uuid4().hex}"


@pytest.mark.usefixtures("browser_manager")
async def test_results_come_back_in_the_shape_the_model_is_promised(
    fake_duckduckgo: FakeDuckDuckGo,
) -> None:
    tool = WebSearchTool(search_url=fake_duckduckgo.url)

    result = await tool.execute({"query": _query()}, conversation_id=uuid.uuid4())

    assert not result.is_error, result.error
    results = result.data["results"]
    assert len(results) == MAX_RESULTS
    assert results[0] == {
        "title": "Результат 0",
        "url": "https://site0.example/page",
        "snippet": "Фрагмент номер 0",
    }
    assert [item["url"] for item in results] == [
        f"https://site{index}.example/page" for index in range(MAX_RESULTS)
    ]


async def test_one_conversation_reuses_its_browser_session_across_searches(
    browser_manager: BrowserSessionManager[BrowserContext], fake_duckduckgo: FakeDuckDuckGo
) -> None:
    tool = WebSearchTool(search_url=fake_duckduckgo.url)
    conversation_id = uuid.uuid4()
    query = _query()
    sessions_before = browser_manager.active_sessions

    await tool.execute({"query": query}, conversation_id=conversation_id)
    await tool.execute({"query": query}, conversation_id=conversation_id)

    first, second = fake_duckduckgo.seen(query)
    assert first.visitor is None
    assert first.issued is not None
    assert second.visitor == first.issued
    assert browser_manager.active_sessions == sessions_before + 1


async def test_different_conversations_search_in_isolated_browser_sessions(
    browser_manager: BrowserSessionManager[BrowserContext], fake_duckduckgo: FakeDuckDuckGo
) -> None:
    tool = WebSearchTool(search_url=fake_duckduckgo.url)
    query = _query()
    sessions_before = browser_manager.active_sessions

    await tool.execute({"query": query}, conversation_id=uuid.uuid4())
    await tool.execute({"query": query}, conversation_id=uuid.uuid4())

    first, second = fake_duckduckgo.seen(query)
    assert first.visitor is None
    assert second.visitor is None
    assert second.issued != first.issued
    assert browser_manager.active_sessions == sessions_before + 2


@pytest.mark.usefixtures("browser_manager")
async def test_the_search_page_is_closed_after_the_call(fake_duckduckgo: FakeDuckDuckGo) -> None:
    conversation_id = uuid.uuid4()

    await WebSearchTool(search_url=fake_duckduckgo.url).execute(
        {"query": _query()}, conversation_id=conversation_id
    )

    async with browser_context(conversation_id) as context:
        assert context.pages == []


@pytest.mark.usefixtures("browser_manager")
@pytest.mark.parametrize(
    ("query", "error"),
    [
        ("blocked", SEARCH_BLOCKED_TEXT),
        ("forbidden", SEARCH_BLOCKED_TEXT),
        ("redesigned", UNRECOGNIZED_PAGE_TEXT),
    ],
)
async def test_a_page_without_results_is_never_passed_off_as_an_empty_search(
    fake_duckduckgo: FakeDuckDuckGo, query: str, error: str
) -> None:
    result = await WebSearchTool(search_url=fake_duckduckgo.url).execute(
        {"query": query}, conversation_id=uuid.uuid4()
    )

    assert result.is_error
    assert result.error == error


@pytest.mark.usefixtures("browser_manager")
async def test_an_http_error_is_reported_with_its_status(fake_duckduckgo: FakeDuckDuckGo) -> None:
    result = await WebSearchTool(search_url=fake_duckduckgo.url).execute(
        {"query": "server-error"}, conversation_id=uuid.uuid4()
    )

    assert result.is_error
    assert result.error is not None
    assert "500" in result.error


@pytest.mark.usefixtures("browser_manager")
async def test_a_search_that_found_nothing_is_a_success_with_no_results(
    fake_duckduckgo: FakeDuckDuckGo,
) -> None:
    result = await WebSearchTool(search_url=fake_duckduckgo.url).execute(
        {"query": "nothing"}, conversation_id=uuid.uuid4()
    )

    assert not result.is_error, result.error
    assert result.data == {"results": []}


@pytest.mark.usefixtures("browser_manager")
async def test_an_unreachable_search_engine_is_an_error_not_a_crash() -> None:
    with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as server:
        host, port = server.server_address[:2]
    closed_port_url = f"http://{host!s}:{port}/html/"

    result = await WebSearchTool(search_url=closed_port_url).execute(
        {"query": _query()}, conversation_id=uuid.uuid4()
    )

    assert result.is_error
    assert result.error == SEARCH_UNAVAILABLE_TEXT


@pytest.mark.usefixtures("browser_manager")
@pytest.mark.skipif(
    os.getenv("CI") is not None,
    reason=(
        "живой запрос к внешнему DuckDuckGo никогда не запускается в CI, независимо от того, "
        "заблокирует ли DDG конкретный раннер, — раннер без sandbox не должен зависеть от "
        "стороннего антибот-поведения, см. architecture.md, раздел «Песочница Chromium»"
    ),
)
async def test_a_real_search_returns_results_in_the_promised_shape() -> None:
    result = await WebSearchTool().execute({"query": LIVE_QUERY}, conversation_id=uuid.uuid4())

    if result.error == SEARCH_BLOCKED_TEXT:
        pytest.skip(
            "DuckDuckGo отклонил автоматический запрос с этой сети: форму выдачи не проверить"
        )
    if result.error == SEARCH_UNAVAILABLE_TEXT:
        pytest.skip("DuckDuckGo недоступен по сети")

    assert not result.is_error, result.error
    results = result.data["results"]
    assert 1 <= len(results) <= MAX_RESULTS
    for item in results:
        assert set(item) == {"title", "url", "snippet"}
        assert item["title"].strip()
        assert urlsplit(item["url"]).scheme in {"http", "https"}
        assert urlsplit(item["url"]).netloc
        assert "duckduckgo.com" not in urlsplit(item["url"]).netloc
