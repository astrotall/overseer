from __future__ import annotations

import uuid
from typing import Any

from libs.confirmations import ConfirmationRequiredError, ConfirmationStore
from libs.core.logging import get_logger
from libs.llm.base import ToolCall
from libs.tools import Tool, ToolResult

logger = get_logger(__name__)


class PendingConfirmationHandler:
    def __init__(self, store: ConfirmationStore) -> None:
        self._store = store

    async def __call__(
        self, conversation_id: uuid.UUID, tool: Tool[Any], call: ToolCall
    ) -> ToolResult:
        summary = tool.describe_call(call.arguments)
        confirmation_id = await self._store.create_pending(
            conversation_id,
            tool_call_id=call.id,
            tool_name=tool.name,
            arguments=call.arguments,
            summary=summary,
        )
        logger.info(
            "chat.confirmation_pending_created",
            conversation_id=str(conversation_id),
            tool=tool.name,
            tool_call_id=call.id,
            confirmation_id=str(confirmation_id),
        )
        raise ConfirmationRequiredError(confirmation_id, summary)
