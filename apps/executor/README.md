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

1. В корневом `.env` (значения сгенерировать, например `openssl rand -hex 32`):

   ```dotenv
   REDIS_EXECUTOR_PASSWORD=<пароль>
   EXECUTOR_REDIS_URL=redis://overseer-executor:<тот же пароль>@127.0.0.1:56379/0
   ```

   Адрес — `127.0.0.1`, не `localhost`. Остальные `EXECUTOR_*` — в `.env.example`, по умолчанию
   файлы открываются и сохраняются только в `%USERPROFILE%\Documents\Overseer` (папку executor
   создаёт сам).
2. Пересоздать Redis, чтобы он завёл пользователя `overseer-executor`:
   `docker compose -f docker/docker-compose.yml up -d --force-recreate redis`.
3. Поставить зависимости и запустить:

   ```powershell
   uv sync --group executor
   uv run python -m apps.executor.main
   ```

Остановка — Ctrl+C: приём прекращается, текущий вызов доигрывается, владение очередью снимается,
Excel закрывается, книги с несохранёнными изменениями остаются открытыми у пользователя. Второй
Ctrl+C — немедленный выход. Второй экземпляр на том же Redis отказывается стартовать; сразу после
падения новый ждёт до 15 с, пока истечёт владение упавшего.

Тесты с живым Excel и с настоящими junction'ами: `uv run pytest -m windows`. **На Windows они ещё
не запускались** — что именно не проверено, перечислено в architecture-executor.md.

## Как общается с `api`

Спроектировано в OVE-29, реализовано в OVE-60: `api` кладёт запрос в Redis-список
`overseer:executor:requests`, executor забирает его, отвечает `accepted` и результатом в
`overseer:executor:reply:<id>` и раз в 5 с продлевает ключи владения и heartbeat — по heartbeat `api`
узнаёт, запущен ли executor. Доставка at-most-once, операции — закрытый allowlist, разговоры
работают каждый со своей книгой. Входящий порт на машине пользователя не нужен: соединение
инициирует executor, и ходит он в Redis отдельным пользователем, которому доступны только ключи
`overseer:executor:*` и только нужные команды (`docker/redis/executor.acl`).

```text
main.py      точка входа          lease.py     владение очередью и heartbeat
config.py    ExecutorSettings     consumer.py  цикл приёма и мягкая остановка
dispatch.py  тип запроса → обработчик
paths.py     разрешённые папки    excel/sessions.py  книга на разговор
```

Решение целиком, замеры и что не проверено — в
[.claude/knowledge/architecture-executor.md](../../.claude/knowledge/architecture-executor.md),
раздел «Транспорт `apps/api` ↔ `apps/executor`», подраздел «Реализация (OVE-60)».
