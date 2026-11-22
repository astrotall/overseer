# apps/executor — «руки» агента на Windows-машине пользователя

## Что это

`executor` — **отдельный процесс, который работает вне Docker, прямо в Windows-сессии
пользователя**. Автоматизация десктопа принципиально не запускается в контейнере:

- **COM / win32com** (Word, Excel, Outlook) — требует нативной Windows и интерактивной
  пользовательской сессии;
- **Playwright с реальным профилем браузера** — нужен доступ к профилю пользователя;
- **клавиатура/мышь/окна/скриншоты** — нужен доступ к рабочему столу.

В Docker-сборку и `docker-compose.yml` executor не входит.

## Что уже есть

`ExcelController` (OVE-41) — тонкая обёртка над Excel через win32com, слоем **под**
инструментами: открыть или создать книгу, записать диапазон, сохранить.

```text
com.py              платформенный гард, ленивый импорт pywin32, HRESULT → исключения Office*
sta.py              StaThread: все COM-вызовы на одном выделенном STA-потоке
excel/validation.py чистый код: A1, листы, данные, формулы, пути
excel/backend.py    порт ExcelBackend
excel/controller.py ExcelController
excel/com_backend.py Win32ExcelBackend поверх win32com
```

Все модули импортируются на любой ОС; настоящий COM нужен только `Win32ExcelBackend`, и вне
Windows его первый запуск — `ConfigurationError`. Решения (свой экземпляр Excel через
`DispatchEx`, STA-поток, «не трогай Excel пользователя», формулы как текст по умолчанию,
исключения) — в [.claude/knowledge/architecture-executor.md](../../.claude/knowledge/architecture-executor.md).

## Запуск на Windows

```powershell
uv sync --group executor
uv run pytest -m windows
```

Тесты с живым Excel написаны, но **на Windows ещё не запускались** — что именно не проверено,
перечислено в architecture-executor.md.

## Как общается с `api`

Спроектировано в OVE-29: `api` кладёт запрос в Redis-список `overseer:executor:requests`, executor
забирает его, отвечает `accepted` и результатом в `overseer:executor:reply:<id>` и раз в 5 с
обновляет heartbeat-ключ с TTL — по нему `api` узнаёт, запущен ли executor. Доставка at-most-once,
операции — закрытый allowlist. Входящий порт на машине пользователя не нужен: соединение
инициирует executor.

Контракт сообщений — `libs/schemas/executor.py`, порт клиента и фейк — `libs/executor/`. Цикла
приёма и `ExecutorSettings` ещё нет. До их реализации Redis обязан быть закрыт: сейчас compose
публикует его на всех интерфейсах без пароля. Решение целиком — в
[.claude/knowledge/architecture-executor.md](../../.claude/knowledge/architecture-executor.md),
раздел «Транспорт `apps/api` ↔ `apps/executor`».
