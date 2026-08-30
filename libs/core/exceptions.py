from __future__ import annotations


class OverseerError(Exception):
    default_message = "Внутренняя ошибка Overseer"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message


class ConfigurationError(OverseerError):
    default_message = "Некорректная конфигурация"


class NotFoundError(OverseerError):
    default_message = "Ресурс не найден"


class ExternalServiceError(OverseerError):
    default_message = "Ошибка внешнего сервиса"
