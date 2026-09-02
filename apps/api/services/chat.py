from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from libs.core.logging import get_logger
from libs.db.repositories import ConversationRepository
from libs.llm.base import ChatMessage, LLMClient
from libs.llm.system_prompt import get_system_prompt_message

logger = get_logger(__name__)

DEFAULT_HISTORY_LIMIT = 29


class ChatService:
    def __init__(
        self,
        session: AsyncSession,
        llm_client: LLMClient,
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> None:
        if history_limit <= 0:
            raise ValueError(f"history_limit must be positive, got {history_limit}")
        if history_limit % 2 == 0:
            raise ValueError(
                "history_limit must be odd, got "
                f"{history_limit}: history is a strict user/assistant sequence ending on "
                "the fresh user message, so a slice taken from the end must start on "
                "'user' too — that only holds for an odd-sized slice"
            )

        self._session = session
        self._repository = ConversationRepository(session)
        self._llm_client = llm_client
        self._history_limit = history_limit

    async def send_message(self, conversation_id: uuid.UUID, text: str) -> ChatMessage:
        try:
            await self._repository.append_message(
                conversation_id, ChatMessage(role="user", content=text)
            )
            history = await self._repository.get_history(conversation_id, limit=self._history_limit)
            response = await self._llm_client.complete([get_system_prompt_message(), *history])

            if response.has_tool_calls:
                logger.warning(
                    "chat.tool_calls_dropped",
                    conversation_id=str(conversation_id),
                    tools=[call.name for call in response.tool_calls],
                )

            answer = ChatMessage(role="assistant", content=response.text)
            await self._repository.append_message(conversation_id, answer)

            logger.info(
                "chat.turn_completed",
                conversation_id=str(conversation_id),
                model=response.model,
                stop_reason=response.stop_reason,
                history_size=len(history),
            )
            await self._session.commit()
            return answer
        except Exception:
            await self._session.rollback()
            raise
