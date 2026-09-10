from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Final

from libs.browser.session import BrowserSessionManager
from libs.core.config import Settings
from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Playwright

logger = get_logger(__name__)

CONTAINER_LAUNCH_ARGS: Final[tuple[str, ...]] = ("--disable-dev-shm-usage",)

PLAYWRIGHT_MISSING = (
    "Playwright не установлен: браузерные инструменты недоступны. "
    "Поставьте группу browser — uv sync --group browser && uv run playwright install chromium"
)


class PlaywrightBrowserBackend:
    def __init__(
        self,
        *,
        headless: bool = True,
        no_sandbox: bool = False,
        launch_args: Sequence[str] = CONTAINER_LAUNCH_ARGS,
    ) -> None:
        self._headless = headless
        self._sandbox = not no_sandbox
        self._launch_args = list(launch_args)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    @property
    def running(self) -> bool:
        return self._browser is not None

    @property
    def connected(self) -> bool:
        browser = self._browser
        return browser is not None and browser.is_connected()

    async def new_context(self) -> BrowserContext:
        browser = await self._ensure_browser()
        return await browser.new_context()

    async def aclose(self) -> None:
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None

        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.exception("browser.close_failed")
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                logger.exception("browser.driver_stop_failed")

    async def _ensure_browser(self) -> Browser:
        browser = self._browser
        if browser is not None:
            if self.connected:
                return browser
            logger.warning("browser.reconnecting_after_crash")
            await self.aclose()

        if self._playwright is None:
            self._playwright = await self._start_playwright()

        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=self._launch_args,
            chromium_sandbox=self._sandbox,
        )
        logger.info(
            "browser.launched",
            headless=self._headless,
            sandbox=self._sandbox,
            version=self._browser.version,
        )
        return self._browser

    async def _start_playwright(self) -> Playwright:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ConfigurationError(PLAYWRIGHT_MISSING) from exc

        return await async_playwright().start()


def create_browser_manager(settings: Settings) -> BrowserSessionManager[BrowserContext]:
    backend = PlaywrightBrowserBackend(
        headless=settings.browser_headless,
        no_sandbox=settings.browser_no_sandbox,
    )
    return BrowserSessionManager(
        backend,
        idle_ttl_seconds=settings.browser_idle_ttl_seconds,
        sweep_interval_seconds=settings.browser_sweep_interval_seconds,
        max_sessions=settings.browser_max_sessions,
    )


_manager: BrowserSessionManager[BrowserContext] | None = None


def init_browser_manager(settings: Settings) -> BrowserSessionManager[BrowserContext]:
    global _manager

    if _manager is not None:
        return _manager

    _manager = create_browser_manager(settings)
    return _manager


def get_browser_manager() -> BrowserSessionManager[BrowserContext]:
    if _manager is None:
        raise ConfigurationError(
            "BrowserSessionManager не инициализирован: вызовите init_browser_manager()"
        )
    return _manager


def reset_browser_manager() -> None:
    global _manager
    _manager = None


def browser_context(conversation_id: uuid.UUID) -> AbstractAsyncContextManager[BrowserContext]:
    return get_browser_manager().acquire(conversation_id)
