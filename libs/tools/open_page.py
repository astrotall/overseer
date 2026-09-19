from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

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
  return {
    title: clip(document.title, maxTitleChars),
    paragraphs,
    fallback: container && paragraphs.length === 0 ? clip(container.innerText, maxChars) : "",
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
        "Открывает только адреса публичного интернета."
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

        failed_navigations: list[str] = []
        navigation_responses: list[Response] = []

        def remember_failed_navigation(request: Request) -> None:
            if request.is_navigation_request() and request.frame == page.main_frame:
                failed_navigations.append(request.url)

        def remember_navigation_response(response: Response) -> None:
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                navigation_responses.append(response)

        page.on("requestfailed", remember_failed_navigation)
        page.on("response", remember_navigation_response)
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            logger.warning("open_page.navigation_timed_out", timeout_ms=NAVIGATION_TIMEOUT_MS)
            return ToolResult.failed(page_timeout_text(url), summary=PAGE_TIMEOUT_SUMMARY)
        except PlaywrightError as exc:
            logger.warning("open_page.navigation_failed", error=type(exc).__name__)
            if failed_navigations:
                refusal = await _refuse_non_public(guard, failed_navigations[-1])
                if refusal is not None:
                    return refusal
            if navigation_responses and _is_file_download(navigation_responses[-1]):
                logger.warning("open_page.file_download_refused")
                return ToolResult.failed(NOT_HTML_TEXT)
            return ToolResult.failed(PAGE_UNAVAILABLE_TEXT)

        status = response.status if response is not None else None
        if status is not None and status >= 400:
            logger.warning("open_page.http_error", status=status)
            return ToolResult.failed(
                f"Страница ответила ошибкой HTTP {status}. Открыть не удалось."
            )

        content_type = response.headers.get("content-type", "") if response is not None else ""
        if not _is_html(content_type):
            logger.warning("open_page.not_html", content_type=content_type)
            return ToolResult.failed(NOT_HTML_TEXT)

        try:
            raw = await extract_page(page)
        except PlaywrightError as exc:
            logger.warning("open_page.extraction_failed", error=type(exc).__name__)
            return ToolResult.failed(PAGE_CHANGED_TEXT)
        summary = build_page_summary(
            url=page.url,
            title=raw.get("title", ""),
            paragraphs=raw.get("paragraphs", []),
            fallback=raw.get("fallback", ""),
        )

        logger.info(
            "open_page.completed",
            title_length=len(summary.title),
            content_length=len(summary.content),
        )
        return ToolResult.ok(
            summary=summary.title or "Страница открыта, заголовка нет",
            data=summary.model_dump(),
        )


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


def build_page_summary(
    *, url: str, title: str, paragraphs: Sequence[str], fallback: str
) -> PageSummary:
    cleaned_paragraphs = [text for text in (_clean(p) for p in paragraphs) if text]
    content = (
        "\n\n".join(cleaned_paragraphs[:MAX_PARAGRAPHS]) if cleaned_paragraphs else _clean(fallback)
    )
    return PageSummary(
        title=_truncate(_clean(title), MAX_TITLE_CHARS),
        url=url,
        content=_truncate(content, MAX_CONTENT_CHARS),
    )


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
