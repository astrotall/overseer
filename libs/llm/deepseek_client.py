from __future__ import annotations

from collections.abc import Sequence

from libs.core.config import Settings, get_settings
from libs.core.exceptions import ConfigurationError
from libs.llm.base import ChatMessage, LLMClient, LLMResponse, ToolSpec


class DeepSeekClient(LLMClient):
    default_model = "deepseek-chat"

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        if not settings.deepseek_api_key:
            raise ConfigurationError("Не задан DEEPSEEK_API_KEY")
        self._api_key = settings.deepseek_api_key
        self._base_url = settings.deepseek_base_url

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        raise NotImplementedError("Клиент DeepSeek будет реализован отдельной задачей")
