from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Protocol

from libs.schemas.executor import (
    ErrorOutcome,
    ExecutorError,
    ExecutorErrorCode,
    ExecutorRequest,
    ExecutorResponse,
)
from libs.tools.base import ToolResult

DEFAULT_PICKUP_TIMEOUT_S: Final = 5.0
DEFAULT_CALL_BUDGET_S: Final = 70.0

NOT_EXECUTED_SUMMARY: Final = "Действие на компьютере пользователя не выполнено"
UNKNOWN_OUTCOME_SUMMARY: Final = "Результат действия на компьютере пользователя неизвестен"

UNKNOWN_OUTCOME_TEXT: Final = (
    "Действие на компьютере пользователя не подтвердило завершение: выполнилось ли оно, "
    "неизвестно. Не повторяй вызов автоматически — повтор может записать или сохранить "
    "файл второй раз. Скажи пользователю, что результат нужно проверить."
)

FAILURE_TEXTS: Final[Mapping[ExecutorErrorCode, str]] = {
    "executor_unavailable": (
        "Модуль управления компьютером пользователя (executor) не запущен или не отвечает: "
        "действие не выполнено. Скажи пользователю, что executor нужно запустить."
    ),
    "transport_unavailable": (
        "Канал связи с компьютером пользователя недоступен: действие не выполнено. "
        "Повтори позже или сообщи пользователю."
    ),
    "not_picked_up": (
        "Компьютер пользователя занят другой операцией или не взял запрос вовремя: "
        "действие не выполнено. Его можно повторить позже."
    ),
    "reply_timeout": UNKNOWN_OUTCOME_TEXT,
    "malformed_reply": UNKNOWN_OUTCOME_TEXT,
    "transport_error": UNKNOWN_OUTCOME_TEXT,
    "payload_too_large": (
        "Запрос слишком большой для передачи на компьютер пользователя: действие не "
        "выполнено. Раздели его на части поменьше."
    ),
    "malformed_request": (
        "Компьютер пользователя отклонил запрос некорректной формы: действие не выполнено."
    ),
    "unsupported_version": (
        "Версия executor на компьютере пользователя не совпадает с версией сервера: "
        "действие не выполнено. Скажи пользователю, что executor нужно обновить."
    ),
    "unknown_operation": (
        "Executor на компьютере пользователя не знает такой операции: действие не "
        "выполнено. Возможно, executor нужно обновить."
    ),
    "deadline_exceeded": (
        "Запрос дошёл до компьютера пользователя слишком поздно и был отброшен: действие "
        "не выполнено. Его можно повторить."
    ),
    "path_not_allowed": (
        "Этот путь находится вне папок, с которыми агенту разрешено работать: действие не "
        "выполнено. Попроси пользователя выбрать файл в разрешённой папке."
    ),
    "internal_error": UNKNOWN_OUTCOME_TEXT,
    "invalid_input": (
        "Приложение Office отклонило данные запроса: действие не выполнено. Исправь "
        "аргументы и повтори."
    ),
    "file_exists": (
        "Файл по этому пути уже существует, а перезапись не запрошена: действие не "
        "выполнено. Спроси пользователя, перезаписать ли файл."
    ),
    "unsaved_changes": (
        "В открытом документе есть несохранённые изменения: действие не выполнено. "
        "Сначала сохрани документ."
    ),
    "not_installed": (
        "Нужное приложение Office не установлено на компьютере пользователя: действие не выполнено."
    ),
    "busy": (
        "Приложение Office занято (например, пользователь редактирует ячейку) и отклонило "
        "вызов: действие не выполнено. Его можно повторить позже."
    ),
    "file_locked": (
        "Файл занят другим процессом или открыт только для чтения: действие не выполнено."
    ),
    "timeout": UNKNOWN_OUTCOME_TEXT,
    "com_error": UNKNOWN_OUTCOME_TEXT,
}

OUTCOME_SUMMARIES: Final[Mapping[ErrorOutcome, str]] = {
    "not_executed": NOT_EXECUTED_SUMMARY,
    "unknown": UNKNOWN_OUTCOME_SUMMARY,
}


class ExecutorClient(Protocol):
    async def call(self, request: ExecutorRequest) -> ExecutorResponse: ...


def failure_result(error: ExecutorError) -> ToolResult:
    return ToolResult.failed(FAILURE_TEXTS[error.code], summary=OUTCOME_SUMMARIES[error.outcome])
