from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.main import lifespan
from libs.core.config import Settings
from libs.core.exceptions import ConfigurationError


def test_api_startup_fails_fast_when_active_provider_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(llm_provider="deepseek", deepseek_api_key=None, anthropic_api_key=None)
    monkeypatch.setattr("apps.api.main.get_settings", lambda: settings)

    app = FastAPI(lifespan=lifespan)

    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"), TestClient(app):
        pass
