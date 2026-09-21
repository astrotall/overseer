from __future__ import annotations

import asyncio
import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from libs.browser import EgressDeniedError, EgressGuard, browser_page, get_egress_guard
from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger
from libs.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from playwright.async_api import Page, Request, Response

logger = get_logger(__name__)

MAX_URL_LENGTH: Final = 2048
MAX_TITLE_CHARS: Final = 200
MAX_CONTENT_CHARS: Final = 1500
MAX_PARAGRAPHS: Final = 3
MAX_URL_IN_ERROR_CHARS: Final = 200
NAVIGATION_TIMEOUT_MS: Final = 15_000
REFRESH_TIMEOUT_MS: Final = 15_000
MAX_REFRESH_DELAY_S: Final = 5
MAX_REFRESH_HOPS: Final = 3

HTML_WHITESPACE: Final = " \t\n\f\r"
URL_EDGE_CHARACTERS: Final = "".join(chr(code) for code in range(0x21))
DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}

EXTRACT_LIMITS: Final[dict[str, int]] = {
    "maxParagraphs": MAX_PARAGRAPHS,
    "maxChars": MAX_CONTENT_CHARS + 1,
    "maxTitleChars": MAX_TITLE_CHARS + 1,
}

EXTRACT_PAGE_JS: Final = """
({ maxParagraphs, maxChars, maxTitleChars }) => {
  const clip = (value, limit) => {
    let clipped = "";
    let count = 0;
    for (const symbol of (value || "").replace(/\\s+/g, " ").trim()) {
      if (count === limit) break;
      clipped += symbol;
      count += 1;
    }
    return clipped;
  };
  const container = document.querySelector("main, article") || document.body;
  const paragraphs = [];
  if (container) {
    for (const paragraph of container.querySelectorAll("p")) {
      if (paragraphs.length === maxParagraphs) break;
      const text = clip(paragraph.innerText, maxChars);
      if (text) paragraphs.push(text);
    }
  }
  const refresh = Array.from(document.querySelectorAll("meta[http-equiv]")).find(
    (meta) => meta.httpEquiv.trim().toLowerCase() === "refresh"
  );
  return {
    title: clip(document.title, maxTitleChars),
    paragraphs,
    fallback: container && paragraphs.length === 0 ? clip(container.innerText, maxChars) : "",
    refresh: refresh ? refresh.content : null,
    baseUrl: document.baseURI,
  };
}
"""

PAGE_UNAVAILABLE_TEXT: Final = (
    "Страница не ответила: адрес не существует, нет сети или соединение отклонено. "
    "Открыть не удалось."
)
PAGE_TIMEOUT_SUMMARY: Final = "Страница не загрузилась вовремя"
PAGE_CHANGED_TEXT: Final = (
    "Страница сама перешла на другой адрес, пока её читали, и содержимое извлечь не удалось. "
    "Повтори вызов один раз; если снова не выйдет — скажи пользователю."
)
NOT_HTML_TEXT: Final = (
    "Страница вернула не HTML-содержимое, извлечь текст нельзя. Открыть не удалось."
)
EXTRACTION_TIMEOUT_TEXT: Final = (
    f"Страница загрузилась, но не отдала содержимое за {REFRESH_TIMEOUT_MS / 1000:g} с. "
    "Открыть не удалось."
)
REFRESH_TIMEOUT_SUMMARY: Final = "Страница-заглушка не перешла на целевой адрес вовремя"
REFRESH_CHAIN_SUMMARY: Final = "Слишком длинная цепочка перенаправлений"
REFRESH_CHAIN_TEXT: Final = (
    f"Страница перенаправляет через <meta refresh> по цепочке длиннее {MAX_REFRESH_HOPS} "
    "переходов, дальше инструмент не пошёл. Открыть не удалось."
)
BLOCKED_ADDRESS_TEXT: Final = (
    "Адрес ведёт не в публичный интернет, а во внутреннюю сеть (loopback, частный, "
    "link-local или зарезервированный диапазон) — сам или через перенаправление. Открывать "
    "такие адреса запрещено. Не повторяй вызов с этим адресом или с другим именем того же ресурса."
)


class PageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    url: str
    content: str
    refresh_url: str | None = None


@dataclass(frozen=True, slots=True)
class Refresh:
    delay_s: int
    url: str | None


class Departure(enum.Enum):
    LANDED = "landed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class _NavigationLog:
    def __init__(self, page: Page) -> None:
        self.requested: list[str] = []
        self.failed: list[str] = []
        self.responses: list[Response] = []
        self._page = page
        self._failure = asyncio.Event()
        page.on("request", self._remember_request)
        page.on("requestfailed", self._remember_failed)
        page.on("response", self._remember_response)

    def _remember_request(self, request: Request) -> None:
        if request.is_navigation_request() and request.frame == self._page.main_frame:
            self.requested.append(request.url)

    def _remember_failed(self, request: Request) -> None:
        if request.is_navigation_request() and request.frame == self._page.main_frame:
            self.failed.append(request.url)
            self._failure.set()

    def _remember_response(self, response: Response) -> None:
        if response.request.is_navigation_request() and response.frame == self._page.main_frame:
            self.responses.append(response)

    async def wait_for_departure(
        self, departed_from: str | None, failures_before: int, timeout_s: float
    ) -> Departure:
        if len(self.failed) > failures_before:
            return Departure.FAILED
        if timeout_s <= 0:
            return Departure.TIMED_OUT
        self._failure.clear()
        arrival = asyncio.ensure_future(
            self._page.wait_for_load_state("domcontentloaded", timeout=timeout_s * 1000)
            if departed_from is None
            else self._page.wait_for_url(
                lambda url: url != departed_from,
                wait_until="domcontentloaded",
                timeout=timeout_s * 1000,
            )
        )
        failure = asyncio.ensure_future(self._failure.wait())
        try:
            await asyncio.wait(
                {arrival, failure}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            arrival.cancel()
            failure.cancel()
            outcomes = await asyncio.gather(arrival, failure, return_exceptions=True)
        if len(self.failed) > failures_before:
            return Departure.FAILED
        if not isinstance(outcomes[0], BaseException):
            return Departure.LANDED
        return Departure.TIMED_OUT


class OpenPageArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(
        min_length=1,
        max_length=MAX_URL_LENGTH,
        description="Полный адрес страницы, включая схему — например, https://example.com",
    )

    @field_validator("url")
    @classmethod
    def _require_http_scheme(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("url должен быть полным адресом со схемой http:// или https://")
        return value


class OpenPageTool(Tool[OpenPageArguments]):
    name = "open_page"
    description = (
        "Открывает страницу по указанному адресу и возвращает её заголовок и краткое "
        "текстовое содержимое (первые абзацы). Не показывает страницу визуально — "
        "браузер работает без экрана, а извлечённый текст пересказывает модель. "
        "Страницу-заглушку с <meta refresh> инструмент проходит сам и возвращает содержимое "
        "той, на которую она ведёт. Открывает только адреса публичного интернета."
    )
    arguments_model = OpenPageArguments

    async def _execute(
        self, arguments: OpenPageArguments, *, conversation_id: uuid.UUID
    ) -> ToolResult:
        try:
            guard = get_egress_guard()
            refusal = await _refuse_non_public(guard, arguments.url)
            if refusal is not None:
                return refusal

            async with browser_page(conversation_id) as page:
                return await self._open(page, guard, arguments.url)
        except ConfigurationError as exc:
            logger.warning("open_page.browser_unavailable", error=str(exc))
            return ToolResult.failed(f"Открыть страницу не удалось: {exc}")

    async def _open(self, page: Page, guard: EgressGuard, url: str) -> ToolResult:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        navigations = _NavigationLog(page)
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            logger.warning("open_page.navigation_timed_out", timeout_ms=NAVIGATION_TIMEOUT_MS)
            return ToolResult.failed(page_timeout_text(url), summary=PAGE_TIMEOUT_SUMMARY)
        except PlaywrightError as exc:
            logger.warning("open_page.navigation_failed", error=type(exc).__name__)
            return await _explain_failed_navigation(guard, navigations)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + REFRESH_TIMEOUT_MS / 1000
        for hop in range(MAX_REFRESH_HOPS + 1):
            refusal = _refuse_response(response)
            if refusal is not None:
                return refusal

            departed_from = page.url
            requests_before = len(navigations.requested)
            failures_before = len(navigations.failed)
            responses_before = len(navigations.responses)
            target: str | None = None
            try:
                raw = await asyncio.wait_for(extract_page(page), deadline - loop.time())
            except TimeoutError:
                pending = navigations.requested[requests_before:]
                logger.warning("open_page.extraction_timed_out", navigating=bool(pending))
                if pending:
                    return ToolResult.failed(
                        refresh_timeout_text(pending[-1]), summary=REFRESH_TIMEOUT_SUMMARY
                    )
                return ToolResult.failed(EXTRACTION_TIMEOUT_TEXT)
            except PlaywrightError as exc:
                logger.warning("open_page.extraction_failed", error=type(exc).__name__)
            else:
                refresh = parse_refresh(raw.get("refresh") or _refresh_header(response))
                target = refresh_target(refresh, raw.get("baseUrl") or departed_from)
                if len(navigations.failed) > failures_before:
                    return await _explain_failed_navigation(guard, navigations)
                if target is None or refresh is None:
                    return _completed(page.url, raw)
                if refresh.delay_s > MAX_REFRESH_DELAY_S or not _is_followable(target):
                    logger.info("open_page.refresh_not_followed", delay_s=refresh.delay_s)
                    return _completed(page.url, raw, refresh_url=target)
                if hop == MAX_REFRESH_HOPS:
                    logger.warning("open_page.refresh_chain_too_long", hops=hop)
                    return ToolResult.failed(REFRESH_CHAIN_TEXT, summary=REFRESH_CHAIN_SUMMARY)
                refusal = await _refuse_non_public(guard, target)
                if refusal is not None:
                    return refusal
                logger.info("open_page.refresh_detected", delay_s=refresh.delay_s, hop=hop + 1)

            if hop == MAX_REFRESH_HOPS:
                return ToolResult.failed(PAGE_CHANGED_TEXT)

            departure = await navigations.wait_for_departure(
                departed_from if target is not None else None,
                failures_before,
                deadline - loop.time(),
            )
            if departure is Departure.FAILED:
                logger.warning("open_page.refresh_navigation_failed", hop=hop + 1)
                return await _explain_failed_navigation(guard, navigations)
            if departure is Departure.TIMED_OUT:
                logger.warning("open_page.refresh_timed_out", timeout_ms=REFRESH_TIMEOUT_MS)
                if target is None:
                    return ToolResult.failed(PAGE_CHANGED_TEXT)
                return ToolResult.failed(
                    refresh_timeout_text(target), summary=REFRESH_TIMEOUT_SUMMARY
                )
            response = (
                navigations.responses[-1] if len(navigations.responses) > responses_before else None
            )
        return ToolResult.failed(PAGE_CHANGED_TEXT)


def _completed(url: str, raw: dict[str, Any], *, refresh_url: str | None = None) -> ToolResult:
    summary = build_page_summary(
        url=url,
        title=raw.get("title", ""),
        paragraphs=raw.get("paragraphs", []),
        fallback=raw.get("fallback", ""),
        refresh_url=refresh_url,
    )
    logger.info(
        "open_page.completed",
        title_length=len(summary.title),
        content_length=len(summary.content),
        refresh_pending=refresh_url is not None,
    )
    return ToolResult.ok(
        summary=summary.title or "Страница открыта, заголовка нет",
        data=summary.model_dump(exclude_none=True),
    )


def _refresh_header(response: Response | None) -> str:
    return response.headers.get("refresh", "") if response is not None else ""


def _refuse_response(response: Response | None) -> ToolResult | None:
    status = response.status if response is not None else None
    if status is not None and status >= 400:
        logger.warning("open_page.http_error", status=status)
        return ToolResult.failed(f"Страница ответила ошибкой HTTP {status}. Открыть не удалось.")

    content_type = response.headers.get("content-type", "") if response is not None else ""
    if not _is_html(content_type):
        logger.warning("open_page.not_html", content_type=content_type)
        return ToolResult.failed(NOT_HTML_TEXT)
    return None


async def _explain_failed_navigation(guard: EgressGuard, navigations: _NavigationLog) -> ToolResult:
    if navigations.failed:
        refusal = await _refuse_non_public(guard, navigations.failed[-1])
        if refusal is not None:
            return refusal
    if navigations.responses and _is_file_download(navigations.responses[-1]):
        logger.warning("open_page.file_download_refused")
        return ToolResult.failed(NOT_HTML_TEXT)
    return ToolResult.failed(PAGE_UNAVAILABLE_TEXT)


async def extract_page(page: Page) -> dict[str, Any]:
    raw: dict[str, Any] = await page.evaluate(EXTRACT_PAGE_JS, EXTRACT_LIMITS)
    return raw


async def _refuse_non_public(guard: EgressGuard, url: str) -> ToolResult | None:
    try:
        await guard.check_url(url)
    except EgressDeniedError as exc:
        logger.warning("open_page.address_blocked", address=str(exc.address), port=exc.port)
        return ToolResult.failed(BLOCKED_ADDRESS_TEXT)
    except (OSError, ValueError):
        logger.warning("open_page.address_unresolvable")
        return ToolResult.failed(PAGE_UNAVAILABLE_TEXT)
    return None


def page_timeout_text(url: str) -> str:
    shown = _truncate(url, MAX_URL_IN_ERROR_CHARS)
    return (
        f"Страница {shown} не загрузилась за {NAVIGATION_TIMEOUT_MS / 1000:g} с: "
        "сервер не ответил вовремя. Открыть не удалось."
    )


def refresh_timeout_text(target: str) -> str:
    shown = _truncate(target, MAX_URL_IN_ERROR_CHARS)
    return (
        f"Страница — заглушка с <meta refresh>, которая ведёт на {shown}, но переход не "
        f"завершился за {REFRESH_TIMEOUT_MS / 1000:g} с. Содержимое заглушки — не то, что "
        "просили, поэтому оно не возвращается. Открыть не удалось."
    )


def parse_refresh(value: str) -> Refresh | None:
    text = value.lstrip(HTML_WHITESPACE)
    position = 0
    while position < len(text) and text[position] in "0123456789":
        position += 1
    digits = text[:position]
    if not digits and not text[position:].startswith("."):
        return None
    while position < len(text) and text[position] in "0123456789.":
        position += 1
    delay_s = int(digits) if digits else 0

    rest = text[position:]
    if rest:
        if rest[0] not in ";," + HTML_WHITESPACE:
            return None
        rest = rest.lstrip(HTML_WHITESPACE)
        if rest[:1] in {";", ","}:
            rest = rest[1:].lstrip(HTML_WHITESPACE)
    if not rest:
        return Refresh(delay_s=delay_s, url=None)

    url = _refresh_url(rest).strip(URL_EDGE_CHARACTERS)
    return Refresh(delay_s=delay_s, url=url or None)


def refresh_target(refresh: Refresh | None, base_url: str) -> str | None:
    if refresh is None or refresh.url is None:
        return None
    target = urljoin(base_url, refresh.url)
    if _document_key(target) == _document_key(base_url):
        return None
    return target


def build_page_summary(
    *,
    url: str,
    title: str,
    paragraphs: Sequence[str],
    fallback: str,
    refresh_url: str | None = None,
) -> PageSummary:
    cleaned_paragraphs = [text for text in (_clean(p) for p in paragraphs) if text]
    content = (
        "\n\n".join(cleaned_paragraphs[:MAX_PARAGRAPHS]) if cleaned_paragraphs else _clean(fallback)
    )
    return PageSummary(
        title=_truncate(_clean(title), MAX_TITLE_CHARS),
        url=url,
        content=_truncate(content, MAX_CONTENT_CHARS),
        refresh_url=refresh_url,
    )


def _refresh_url(rest: str) -> str:
    if rest[:1] not in {"U", "u"}:
        return _unquote(rest)
    if rest[1:2] not in {"R", "r"} or rest[2:3] not in {"L", "l"}:
        return rest
    after_name = rest[3:].lstrip(HTML_WHITESPACE)
    if not after_name.startswith("="):
        return rest
    return _unquote(after_name[1:].lstrip(HTML_WHITESPACE))


def _unquote(value: str) -> str:
    quote = value[:1]
    if quote not in {"'", '"'}:
        return value
    unquoted = value[1:]
    end = unquoted.find(quote)
    return unquoted if end == -1 else unquoted[:end]


def _document_key(url: str) -> tuple[str, str, int | None, str, str]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port == DEFAULT_PORTS.get(scheme):
        port = None
    return (scheme, (parts.hostname or "").lower(), port, parts.path or "/", parts.query)


def _is_followable(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)


def _is_file_download(response: Response) -> bool:
    if not 200 <= response.status < 300:
        return False
    disposition = response.headers.get("content-disposition", "").strip().lower()
    return disposition.startswith("attachment") or not _is_html(
        response.headers.get("content-type", "")
    )


def _is_html(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not media_type:
        return True
    return "html" in media_type


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"
