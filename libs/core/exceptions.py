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


class ConflictError(OverseerError):
    default_message = "Конфликт состояния: ресурс уже обрабатывается"


class ExternalServiceError(OverseerError):
    default_message = "Ошибка внешнего сервиса"


class LLMError(ExternalServiceError):
    default_message = "Ошибка LLM-провайдера"


class LLMTransientError(LLMError):
    default_message = "Временный сбой LLM-провайдера, запрос можно повторить"


class LLMBadRequestError(LLMError):
    default_message = "LLM-провайдер отклонил запрос"


class LLMResponseError(LLMError):
    default_message = "LLM-провайдер вернул ответ неожиданной формы"


class OfficeError(OverseerError):
    default_message = "Ошибка автоматизации Office"

    def __init__(self, message: str | None = None, *, hresult: int | None = None) -> None:
        super().__init__(message)
        self.hresult = hresult


class OfficeInputError(OfficeError):
    default_message = "Некорректные данные для приложения Office"


class OfficeFileExistsError(OfficeError):
    default_message = "Файл уже существует, перезапись не запрошена"


class OfficeUnsavedChangesError(OfficeError):
    default_message = "В открытом документе есть несохранённые изменения"


class OfficeNotInstalledError(OfficeError):
    default_message = "Приложение Office не установлено или не зарегистрировано в COM"


class OfficeBusyError(OfficeError):
    default_message = "Приложение Office занято и отклонило вызов, его можно повторить позже"


class OfficeFileLockedError(OfficeError):
    default_message = "Файл занят другим процессом или открыт только для чтения"


class OfficeTimeoutError(OfficeError):
    default_message = "Приложение Office не ответило вовремя, результат операции неизвестен"


class OfficeComError(OfficeError):
    default_message = "Приложение Office завершило вызов ошибкой"
