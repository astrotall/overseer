from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest

from libs.browser import BrowserSessionManager, PlaywrightBrowserBackend
from libs.core.config import Settings

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

pytestmark = pytest.mark.browser

IDLE_TTL_S = 60.0
COOKIE_URL = "https://overseer.test/"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def manager(
    settings: Settings, clock: Clock
) -> AsyncIterator[BrowserSessionManager[BrowserContext]]:
    backend = PlaywrightBrowserBackend(headless=True, no_sandbox=settings.browser_no_sandbox)
    manager: BrowserSessionManager[BrowserContext] = BrowserSessionManager(
        backend,
        idle_ttl_seconds=IDLE_TTL_S,
        sweep_interval_seconds=IDLE_TTL_S,
        max_sessions=2,
        clock=clock,
    )
    try:
        async with manager.acquire(uuid.uuid4()):
            pass
    except Exception as exc:
        await manager.aclose()
        if os.getenv("CI"):
            raise
        pytest.skip(f"Chromium недоступен: {exc}")

    yield manager
    await manager.aclose()


async def test_the_context_can_actually_render_a_page(
    manager: BrowserSessionManager[BrowserContext],
) -> None:
    async with manager.acquire(uuid.uuid4()) as context:
        page = await context.new_page()
        await page.set_content("<h1>Отчёт за август</h1>")

        assert await page.title() == ""
        assert await page.inner_text("h1") == "Отчёт за август"
        await page.close()


async def test_one_conversation_keeps_its_cookies_between_calls(
    manager: BrowserSessionManager[BrowserContext],
) -> None:
    conversation_id = uuid.uuid4()

    async with manager.acquire(conversation_id) as context:
        await context.add_cookies(
            [{"name": "session", "value": "kept", "url": COOKIE_URL}],
        )

    async with manager.acquire(conversation_id) as same_context:
        cookies = await same_context.cookies(COOKIE_URL)

    assert [cookie["value"] for cookie in cookies] == ["kept"]


async def test_another_conversation_does_not_see_them(
    manager: BrowserSessionManager[BrowserContext],
) -> None:
    async with manager.acquire(uuid.uuid4()) as context:
        await context.add_cookies(
            [{"name": "session", "value": "kept", "url": COOKIE_URL}],
        )

    async with manager.acquire(uuid.uuid4()) as other_context:
        assert await other_context.cookies(COOKIE_URL) == []


async def test_an_idle_context_is_really_closed_and_a_new_one_replaces_it(
    manager: BrowserSessionManager[BrowserContext], clock: Clock
) -> None:
    conversation_id = uuid.uuid4()
    async with manager.acquire(conversation_id) as context:
        await context.add_cookies(
            [{"name": "session", "value": "kept", "url": COOKIE_URL}],
        )

    clock.advance(IDLE_TTL_S + 1)
    assert await manager.sweep_idle() >= 1

    async with manager.acquire(conversation_id) as fresh_context:
        assert fresh_context is not context
        assert await fresh_context.cookies(COOKIE_URL) == []


async def test_a_closed_manager_leaves_no_browser_behind(settings: Settings) -> None:
    backend = PlaywrightBrowserBackend(headless=True, no_sandbox=settings.browser_no_sandbox)
    manager: BrowserSessionManager[BrowserContext] = BrowserSessionManager(
        backend,
        idle_ttl_seconds=IDLE_TTL_S,
        sweep_interval_seconds=IDLE_TTL_S,
        max_sessions=2,
    )
    try:
        async with manager.acquire(uuid.uuid4()) as context:
            page = await context.new_page()
    except Exception as exc:
        await manager.aclose()
        if os.getenv("CI"):
            raise
        pytest.skip(f"Chromium недоступен: {exc}")

    assert backend.running

    await manager.aclose()

    assert not backend.running
    assert page.is_closed()
