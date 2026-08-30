from __future__ import annotations

from libs.core.config import Settings, get_settings
from libs.core.exceptions import ConfigurationError
from libs.llm.base import ChatMessage, LLMClient, LLMResponse, ToolSpec


class AnthropicClient(LLMClient):
    default_model = "claude-sonnet-4-5"

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        if not settings.anthropic_api_key:
            raise ConfigurationError("Не задан ANTHROPIC_API_KEY")
        self._api_key = settings.anthropic_api_key

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        raise NotImplementedError("Клиент Anthropic будет реализован отдельной задачей")
