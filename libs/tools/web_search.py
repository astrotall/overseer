from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qs, urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from libs.browser import browser_page
from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger
from libs.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = get_logger(__name__)

DUCKDUCKGO_HTML_URL: Final = "https://html.duckduckgo.com/html/"

MAX_RESULTS: Final = 8
MAX_QUERY_LENGTH: Final = 500
MAX_TITLE_CHARS: Final = 200
MAX_SNIPPET_CHARS: Final = 300
NAVIGATION_TIMEOUT_MS: Final = 15_000
EXTRACTION_TIMEOUT_MS: Final = 15_000

RESULT_SELECTOR: Final = ".result"
NO_RESULTS_SELECTOR: Final = ".no-results, .result--no-result"
CHALLENGE_SELECTOR: Final = "#challenge-form, .anomaly-modal__modal"
BLOCKED_STATUSES: Final = frozenset({403, 429})

EXTRACT_RESULTS_JS: Final = """
results => results.map(result => {
  const link = result.querySelector(".result__a");
  const snippet = result.querySelector(".result__snippet");
  return {
    ad: result.classList.contains("result--ad"),
    title: link ? link.innerText : "",
    href: link ? link.getAttribute("href") || "" : "",
    snippet: snippet ? snippet.innerText : "",
  };
})
"""

SEARCH_BLOCKED_TEXT: Final = (
    "DuckDuckGo отклонил автоматический запрос (защита от ботов): поиск сейчас недоступен. "
    "Не повторяй запрос сразу — скажи пользователю, что поиск в интернете временно не работает."
)
SEARCH_UNAVAILABLE_TEXT: Final = (
    "Поисковик не ответил: нет сети или соединение отклонено. Поиск не выполнен."
)
SEARCH_TIMEOUT_SUMMARY: Final = "Поисковик не ответил вовремя"
UNRECOGNIZED_PAGE_TEXT: Final = (
    "Страница поисковика пришла в неизвестном виде, результаты разобрать не удалось. "
    "Поиск не выполнен."
)


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    url: str
    snippet: str


class WebSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(
        min_length=1,
        max_length=MAX_QUERY_LENGTH,
        description="Поисковый запрос — так, как его набрал бы в поисковике человек",
    )


class WebSearchTool(Tool[WebSearchArguments]):
    name = "web_search"
    description = (
        f"Ищет в интернете через DuckDuckGo и возвращает до {MAX_RESULTS} результатов: "
        "заголовок, ссылку и короткий фрагмент текста страницы. Используй, когда для ответа "
        "нужна актуальная информация из сети. Сами страницы по ссылкам не открывает."
    )
    arguments_model = WebSearchArguments

    def __init__(self, *, search_url: str = DUCKDUCKGO_HTML_URL) -> None:
        self._search_url = search_url

    async def _execute(
        self, arguments: WebSearchArguments, *, conversation_id: uuid.UUID
    ) -> ToolResult:
        url = f"{self._search_url}?{urlencode({'q': arguments.query})}"
        try:
            async with browser_page(conversation_id) as page:
                return await self._search(page, url)
        except ConfigurationError as exc:
            logger.warning("web_search.browser_unavailable", error=str(exc))
            return ToolResult.failed(f"Поиск недоступен: {exc}")

    async def _search(self, page: Page, url: str) -> ToolResult:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            logger.warning("web_search.navigation_timed_out", timeout_ms=NAVIGATION_TIMEOUT_MS)
            return ToolResult.failed(
                search_timeout_text(self._search_url), summary=SEARCH_TIMEOUT_SUMMARY
            )
        except PlaywrightError as exc:
            logger.warning("web_search.navigation_failed", error=type(exc).__name__)
            return ToolResult.failed(SEARCH_UNAVAILABLE_TEXT)

        status = response.status if response is not None else None
        try:
            return await asyncio.wait_for(
                self._read(page, status), timeout=EXTRACTION_TIMEOUT_MS / 1000
            )
        except TimeoutError:
            logger.warning("web_search.extraction_timed_out", timeout_ms=EXTRACTION_TIMEOUT_MS)
            return ToolResult.failed(extraction_timeout_text(), summary=SEARCH_TIMEOUT_SUMMARY)

    async def _read(self, page: Page, status: int | None) -> ToolResult:
        if status in BLOCKED_STATUSES or await page.locator(CHALLENGE_SELECTOR).count():
            logger.warning("web_search.blocked", status=status)
            return ToolResult.failed(SEARCH_BLOCKED_TEXT)
        if status is not None and status >= 400:
            logger.warning("web_search.http_error", status=status)
            return ToolResult.failed(f"Поисковик ответил ошибкой HTTP {status}. Поиск не выполнен.")

        raw = await page.eval_on_selector_all(RESULT_SELECTOR, EXTRACT_RESULTS_JS)
        results = parse_results(raw)
        if not results and not await page.locator(NO_RESULTS_SELECTOR).count():
            logger.warning("web_search.unrecognized_page", status=status, raw_results=len(raw))
            return ToolResult.failed(UNRECOGNIZED_PAGE_TEXT)

        logger.info("web_search.completed", results=len(results))
        summary = f"Найдено результатов: {len(results)}" if results else "Поиск ничего не нашёл"
        return ToolResult.ok(
            summary=summary, data={"results": [result.model_dump() for result in results]}
        )


def search_timeout_text(search_url: str) -> str:
    return (
        f"Поисковик {search_url} не ответил за {NAVIGATION_TIMEOUT_MS / 1000:g} с. "
        "Поиск не выполнен: скажи пользователю, что поиск сейчас не отвечает."
    )


def extraction_timeout_text() -> str:
    return (
        f"Страница выдачи загрузилась, но не отдала содержимое за "
        f"{EXTRACTION_TIMEOUT_MS / 1000:g} с. "
        "Поиск не выполнен: скажи пользователю, что поиск сейчас не отвечает."
    )


def parse_results(
    raw: Sequence[Mapping[str, Any]], *, limit: int = MAX_RESULTS
) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in raw:
        if len(results) == limit:
            break
        if item.get("ad"):
            continue

        title = _clean(item.get("title"), MAX_TITLE_CHARS)
        url = resolve_result_url(str(item.get("href") or ""))
        if not title or url is None or url in seen:
            continue

        seen.add(url)
        results.append(
            SearchResult(
                title=title, url=url, snippet=_clean(item.get("snippet"), MAX_SNIPPET_CHARS)
            )
        )
    return results


def resolve_result_url(href: str) -> str | None:
    href = href.strip()
    if href.startswith("//"):
        href = f"https:{href}"

    parts = urlsplit(href)
    if _is_duckduckgo(parts.hostname):
        target = parse_qs(parts.query).get("uddg")
        if not target or _is_duckduckgo(urlsplit(target[0]).hostname):
            return None
        return resolve_result_url(target[0])

    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return href


def _is_duckduckgo(hostname: str | None) -> bool:
    return hostname is not None and (
        hostname == "duckduckgo.com" or hostname.endswith(".duckduckgo.com")
    )


def _clean(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"
