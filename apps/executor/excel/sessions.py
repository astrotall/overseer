from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from typing import Final

from apps.executor.excel.controller import (
    ExcelController,
    SaveResult,
    WorkbookInfo,
    WriteResult,
)
from apps.executor.excel.validation import CellValue
from libs.core.exceptions import OfficeBusyError
from libs.core.logging import get_logger

logger = get_logger(__name__)

SWEEP_INTERVAL_S: Final = 30.0

TOO_MANY_TEXT: Final = (
    "Слишком много разговоров держат открытые книги с несохранёнными изменениями: новую "
    "книгу открыть нельзя, пока одну из них не сохранят"
)

Monotonic = Callable[[], float]


class ConversationWorkbooks:
    def __init__(
        self,
        controller: ExcelController,
        *,
        max_conversations: int,
        idle_ttl_s: float,
        sweep_interval_s: float = SWEEP_INTERVAL_S,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        if max_conversations < 1 or idle_ttl_s <= 0:
            raise ValueError("max_conversations и idle_ttl_s должны быть положительными")
        self._controller = controller
        self._max_conversations = max_conversations
        self._idle_ttl_s = idle_ttl_s
        self._sweep_interval_s = sweep_interval_s
        self._monotonic = monotonic
        self._last_used: dict[uuid.UUID, float] = {}
        self._kept_unsaved: set[uuid.UUID] = set()
        self._next_sweep = monotonic() + sweep_interval_s

    @property
    def open_conversations(self) -> frozenset[uuid.UUID]:
        return frozenset(
            conversation
            for conversation in self._controller.conversations
            if conversation is not None
        )

    async def start(self) -> None:
        self._last_used.clear()
        self._kept_unsaved.clear()
        await self._controller.start()

    async def aclose(self) -> None:
        open_now = self.open_conversations
        if open_now:
            logger.info("excel.conversations_open_at_shutdown", count=len(open_now))
        self._last_used.clear()
        await self._controller.aclose()

    async def open(self, conversation_id: uuid.UUID, path: str | None) -> WorkbookInfo:
        await self._admit(conversation_id)
        try:
            return await self._controller.open_or_create_workbook(
                path, conversation_id=conversation_id
            )
        finally:
            self._touch(conversation_id)

    async def write(
        self,
        conversation_id: uuid.UUID,
        sheet: str,
        start_cell: str,
        data: Sequence[Sequence[CellValue]],
        *,
        formulas: bool,
    ) -> WriteResult:
        self._touch(conversation_id)
        return await self._controller.write_range(
            sheet, start_cell, data, formulas=formulas, conversation_id=conversation_id
        )

    async def save(
        self, conversation_id: uuid.UUID, path: str | None, *, overwrite: bool
    ) -> SaveResult:
        self._touch(conversation_id)
        result = await self._controller.save(
            path, overwrite=overwrite, conversation_id=conversation_id
        )
        self._kept_unsaved.discard(conversation_id)
        return result

    async def sweep(self, *, force: bool = False) -> None:
        now = self._monotonic()
        if not force and now < self._next_sweep:
            return
        self._next_sweep = now + self._sweep_interval_s
        self._forget_closed()

        for conversation in sorted(self.open_conversations, key=self._age_key):
            if now - self._last_used.get(conversation, now) < self._idle_ttl_s:
                continue
            await self._release(conversation, reason="idle")

    async def _admit(self, conversation_id: uuid.UUID) -> None:
        self._forget_closed()
        others = self.open_conversations - {conversation_id}
        if conversation_id in self.open_conversations or len(others) < self._max_conversations:
            return

        for conversation in sorted(others, key=self._age_key):
            if await self._release(conversation, reason="cap"):
                return
        logger.warning("excel.conversation_cap_reached", open=len(others))
        raise OfficeBusyError(TOO_MANY_TEXT)

    async def _release(self, conversation_id: uuid.UUID, *, reason: str) -> bool:
        closed = await self._controller.release(conversation_id)
        if closed:
            self._last_used.pop(conversation_id, None)
            self._kept_unsaved.discard(conversation_id)
            logger.info("excel.conversation_released", reason=reason)
            return True
        if conversation_id not in self._kept_unsaved:
            self._kept_unsaved.add(conversation_id)
            logger.warning(
                "excel.unsaved_workbook_kept",
                conversation_id=str(conversation_id),
                reason=reason,
            )
        return False

    def _forget_closed(self) -> None:
        open_now = self.open_conversations
        for conversation in list(self._last_used):
            if conversation not in open_now:
                self._last_used.pop(conversation)

    def _touch(self, conversation_id: uuid.UUID) -> None:
        self._last_used[conversation_id] = self._monotonic()

    def _age_key(self, conversation_id: uuid.UUID) -> float:
        return self._last_used.get(conversation_id, float("-inf"))
