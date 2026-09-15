from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from libs.browser import browser_context
from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger
from libs.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = get_logger(__name__)

MAX_URL_LENGTH: Final = 2048
MAX_TITLE_CHARS: Final = 200
MAX_CONTENT_CHARS: Final = 1500
MAX_PARAGRAPHS: Final = 3
NAVIGATION_TIMEOUT_MS: Final = 15_000

EXTRACT_PAGE_JS: Final = """
() => {
  const container = document.querySelector("main, article") || document.body;
  const paragraphs = container
    ? Array.from(container.querySelectorAll("p")).map(p => p.innerText)
    : [];
  return {
    title: document.title || "",
    paragraphs,
    fallback: container ? container.innerText : "",
  };
}
"""

PAGE_UNAVAILABLE_TEXT: Final = (
    "Страница не ответила: адрес не существует, нет сети или истекло время ожидания. "
    "Открыть не удалось."
)
NOT_HTML_TEXT: Final = (
    "Страница вернула не HTML-содержимое, извлечь текст нельзя. Открыть не удалось."
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
        "браузер работает без экрана, а извлечённый текст пересказывает модель."
    )
    arguments_model = OpenPageArguments

    async def _execute(
        self, arguments: OpenPageArguments, *, conversation_id: uuid.UUID
    ) -> ToolResult:
        try:
            async with browser_context(conversation_id) as context:
                page = await context.new_page()
                try:
                    return await self._open(page, arguments.url)
                finally:
                    await page.close()
        except ConfigurationError as exc:
            logger.warning("open_page.browser_unavailable", error=str(exc))
            return ToolResult.failed(f"Открыть страницу не удалось: {exc}")

    async def _open(self, page: Page, url: str) -> ToolResult:
        from playwright.async_api import Error as PlaywrightError

        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
            )
        except PlaywrightError as exc:
            logger.warning("open_page.navigation_failed", error=type(exc).__name__)
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

        raw = await page.evaluate(EXTRACT_PAGE_JS)
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
