from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Final

from libs.browser.egress import EgressGuard, EgressProxy
from libs.browser.session import BrowserSessionManager
from libs.core.config import Settings
from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

logger = get_logger(__name__)

PAGE_CLOSE_TIMEOUT_S: Final = 5.0

CONTAINER_LAUNCH_ARGS: Final[tuple[str, ...]] = ("--disable-dev-shm-usage",)
EGRESS_LAUNCH_ARGS: Final[tuple[str, ...]] = (
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
)
PROXIED_LOOPBACK_OVERRIDE_ENV: Final = "PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK"

PLAYWRIGHT_MISSING = (
    "Playwright не установлен: браузерные инструменты недоступны. "
    "Поставьте группу browser — uv sync --group browser && uv run playwright install chromium"
)
PROXIED_LOOPBACK_DISABLED = (
    f"Задана переменная окружения {PROXIED_LOOPBACK_OVERRIDE_ENV}: с ней Chromium ходит на "
    "loopback-адреса мимо прокси исходящего трафика, и защита от SSRF не работает. "
    "Браузер не запускается, пока переменная не снята."
)


class PlaywrightBrowserBackend:
    def __init__(
        self,
        *,
        headless: bool = True,
        no_sandbox: bool = False,
        launch_args: Sequence[str] = CONTAINER_LAUNCH_ARGS,
        egress_guard: EgressGuard | None = None,
    ) -> None:
        self._headless = headless
        self._sandbox = not no_sandbox
        self._launch_args = list(launch_args)
        self._egress_proxy = EgressProxy(egress_guard or EgressGuard())
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
        return await browser.new_context(accept_downloads=False)

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
        try:
            await self._egress_proxy.aclose()
        except Exception:
            logger.exception("browser.egress_proxy_stop_failed")

    async def _ensure_browser(self) -> Browser:
        browser = self._browser
        if browser is not None:
            if self.connected:
                return browser
            logger.warning("browser.reconnecting_after_crash")
            await self.aclose()

        if os.environ.get(PROXIED_LOOPBACK_OVERRIDE_ENV):
            raise ConfigurationError(PROXIED_LOOPBACK_DISABLED)

        if self._playwright is None:
            self._playwright = await self._start_playwright()

        await self._egress_proxy.start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[*self._launch_args, *EGRESS_LAUNCH_ARGS],
            chromium_sandbox=self._sandbox,
            proxy={"server": self._egress_proxy.server_url},
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


def create_browser_manager(
    settings: Settings, *, egress_guard: EgressGuard | None = None
) -> BrowserSessionManager[BrowserContext]:
    backend = PlaywrightBrowserBackend(
        headless=settings.browser_headless,
        no_sandbox=settings.browser_no_sandbox,
        egress_guard=egress_guard,
    )
    return BrowserSessionManager(
        backend,
        idle_ttl_seconds=settings.browser_idle_ttl_seconds,
        sweep_interval_seconds=settings.browser_sweep_interval_seconds,
        max_sessions=settings.browser_max_sessions,
    )


_manager: BrowserSessionManager[BrowserContext] | None = None
_egress_guard: EgressGuard | None = None


def init_browser_manager(
    settings: Settings, *, egress_guard: EgressGuard | None = None
) -> BrowserSessionManager[BrowserContext]:
    global _manager, _egress_guard

    if _manager is not None:
        return _manager

    _egress_guard = egress_guard or EgressGuard()
    _manager = create_browser_manager(settings, egress_guard=_egress_guard)
    return _manager


def get_browser_manager() -> BrowserSessionManager[BrowserContext]:
    if _manager is None:
        raise ConfigurationError(
            "BrowserSessionManager не инициализирован: вызовите init_browser_manager()"
        )
    return _manager


def get_egress_guard() -> EgressGuard:
    if _egress_guard is None:
        raise ConfigurationError(
            "Политика исходящего трафика браузера не инициализирована: "
            "вызовите init_browser_manager()"
        )
    return _egress_guard


def reset_browser_manager() -> None:
    global _manager, _egress_guard
    _manager = None
    _egress_guard = None


def browser_context(conversation_id: uuid.UUID) -> AbstractAsyncContextManager[BrowserContext]:
    return get_browser_manager().acquire(conversation_id)


@asynccontextmanager
async def browser_page(conversation_id: uuid.UUID) -> AsyncIterator[Page]:
    async with browser_context(conversation_id) as context:
        page = await context.new_page()
        try:
            yield page
        finally:
            await _close_page(conversation_id, page)


async def _close_page(conversation_id: uuid.UUID, page: Page) -> None:
    try:
        await asyncio.wait_for(page.close(), PAGE_CLOSE_TIMEOUT_S)
    except TimeoutError:
        logger.warning(
            "browser.page_close_timed_out",
            conversation_id=str(conversation_id),
            timeout_s=PAGE_CLOSE_TIMEOUT_S,
        )
        await get_browser_manager().close_session(conversation_id)
    except Exception as exc:
        logger.warning(
            "browser.page_close_failed",
            conversation_id=str(conversation_id),
            error=type(exc).__name__,
        )
