from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.conftest import database_unavailable_reason, db_engine, redact_database_url

PASSWORD = "s3cret-pw"
URL = f"postgresql+asyncpg://overseer:{PASSWORD}@127.0.0.1:1/overseer_test"


def test_the_password_is_redacted_from_the_shown_database_url() -> None:
    assert redact_database_url(URL) == "postgresql+asyncpg://overseer:***@127.0.0.1:1/overseer_test"


def test_a_url_without_a_password_is_shown_as_is() -> None:
    url = "postgresql+asyncpg://overseer@127.0.0.1:1/overseer_test"

    assert redact_database_url(url) == url


def test_the_password_is_scrubbed_even_from_the_driver_error() -> None:
    reason = database_unavailable_reason(URL, OSError(f"could not connect to {URL}"))

    assert PASSWORD not in reason
    assert "overseer:***@127.0.0.1:1" in reason


async def test_the_skip_message_of_an_unreachable_database_never_shows_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CI", raising=False)
    fixture_func = db_engine.__wrapped__  # type: ignore[attr-defined]
    generator: AsyncIterator[Any] = fixture_func(URL)

    with pytest.raises(pytest.skip.Exception) as skipped:
        await anext(generator)

    assert "Тестовая БД недоступна" in str(skipped.value)
    assert PASSWORD not in str(skipped.value)
