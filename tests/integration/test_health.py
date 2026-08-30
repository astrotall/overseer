from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.integration
async def test_health_returns_ok(async_client: AsyncClient) -> None:
    response = await async_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
