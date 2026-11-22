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

## Как будет общаться с `api` и `worker`

Не решено (OVE-29). Основной кандидат — **Redis pub/sub**: `api`/`worker` публикуют команду в
`overseer:executor:cmd`, executor отвечает в `overseer:executor:result:<request_id>`; плюс в
том, что executor сам инициирует соединение и входящий порт на машине пользователя не нужен.
Запасной вариант — локальный HTTP-сервер на стороне executor. Контроллер от транспорта не
зависит: это класс с async-методами, и как запрос до него доходит, решает OVE-29.
