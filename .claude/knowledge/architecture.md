# Архитектура

Монорепо разделено на **`apps/`** и **`libs/`**:

- **`apps/`** — то, что *запускается*: процессы со своей точкой входа и своим жизненным
  циклом. Каждое приложение — отдельный контейнер или сервис.
- **`libs/`** — то, что *переиспользуется*: общий код без точки входа. Библиотеки ничего
  не знают о том, кто их вызывает, и **не импортируют `apps/`**.

Зависимость всегда однонаправленная: `apps/` → `libs/`. Обратный импорт — ошибка, а не
компромисс; если библиотеке понадобилось что-то из приложения, значит абстракция лежит не
на том слое.

## apps/

### `apps/api` — FastAPI

HTTP + WebSocket: роуты, DI-зависимости, валидация DTO.

- `main.py` — создание `app` и `lifespan` (инициализация и закрытие подключений к
  PostgreSQL и Redis);
- `deps.py` — `SessionDep` / `RedisDep` / `SettingsDep`;
- `routes/health.py` — `GET /health`, отдаёт `{"status": "ok"}`;
- `routes/ws.py` — `/ws`, канал общения с агентом. Сейчас заготовка: держит соединение и
  отражает входящие сообщения (эхо), чтобы можно было проверить транспорт. В финале сюда
  пойдут токены ответа и статусы вызова инструментов.

Запуск: `uv run uvicorn apps.api.main:app --reload`.

### `apps/worker` — Arq

Фоновые и долгие задачи: цепочки рассуждений, инструменты, которые исполняются прямо на
Python (БД, HTTP, файлы). Сейчас — smoke-задача `ping`.

Запуск: `uv run arq apps.worker.main.WorkerSettings`.

### `apps/executor` — заготовка под Windows-сервис

Пустой пакет с README, логики намеренно нет. Это будет **отдельный процесс, работающий вне
Docker, прямо в Windows-сессии пользователя** — «руки» агента на конкретной машине.
Отдельным процессом он нужен потому, что автоматизация десктопа принципиально не
запускается в контейнере:

- **COM / win32com** требует нативной Windows и интерактивной пользовательской сессии;
- **Playwright с реальным профилем браузера** — доступ к профилю, кукам, открытым сессиям;
- клавиатура, мышь, окна, скриншоты — доступ к рабочему столу.

Транспорт до `api`/`worker` ещё не зафиксирован. Основной кандидат — **Redis pub/sub**:
команда публикуется в канал вида `overseer:executor:cmd`, executor слушает, выполняет и
отвечает в `overseer:executor:result:<request_id>`. Плюс в том, что executor сам инициирует
соединение — не нужен входящий порт на машине пользователя. Запасной вариант — локальный
HTTP-сервер на стороне executor. Детали — в `apps/executor/README.md`.

В `docker-compose.yml` executor не входит и входить не должен.

## libs/

| Пакет | Назначение |
|---|---|
| `libs/core` | конфиг (`pydantic-settings`, единый `Settings` + кэшированный `get_settings()`), логи (`structlog`), исключения |
| `libs/db` | SQLAlchemy `Base`, async engine и session, клиент Redis, ORM-модели |
| `libs/llm` | клиенты Anthropic и DeepSeek, провайдер-независимый контракт под tool-calling |
| `libs/tools` | реестр инструментов агента — описание для function calling + исполнитель |
| `libs/schemas` | Pydantic v2 DTO |

### Контракт `libs/llm/base.py`

Зафиксирован (OVE-8). Это нейтральные примитивы, к которым адаптеры приводят оба формата
tool-calling: блоки `tool_use` / `tool_result` у Anthropic и function calls у
OpenAI-совместимых API (DeepSeek — активный провайдер v1). Менять эти типы — ломающее
изменение: правятся все адаптеры разом.

**`ChatMessage`** — одно сообщение истории, плоская форма вместо списка блоков:

| Поле | Когда заполнено |
|---|---|
| `role` | `system` / `user` / `assistant` / `tool` |
| `content` | текст; `None` допустим только у `assistant`, который вызвал инструмент и ничего не сказал |
| `tool_calls` | только у `assistant` — список `ToolCall` |
| `tool_call_id` | только у `tool` — id вызова, на который отвечает это сообщение |
| `is_error` | только у `tool` — инструмент упал, в `content` текст ошибки |

Форма проверяется `model_validator`'ом, лишние поля запрещены (`extra="forbid"`): собрать
сообщение неверной формы нельзя, ошибка вылезет на конструкторе, а не в ответе провайдера.

Плоская форма выбрана потому, что она разворачивается в блоки без потерь, а обратно —
нет. Отсюда обязанности адаптеров:

- **OpenAI-совместимые** (DeepSeek): маппинг почти прямой — `tool_calls` и `role="tool"` с
  `tool_call_id` есть в самом API; `is_error` в протоколе нет, ошибка едет обычным текстом
  в `content`.
- **Anthropic**: `assistant` разворачивается в `[TextBlock, ToolUseBlock, ...]`; идущие
  подряд сообщения `role="tool"` собираются в **одно** `user`-сообщение с несколькими
  `tool_result`-блоками (иначе параллельные вызовы не примутся), `is_error` ложится в
  `tool_result.is_error`; сообщения `role="system"` не отправляются в список сообщений, а
  поднимаются в отдельный параметр `system` запроса.

**`ToolCall`** — `id`, `name`, `arguments` (уже распарсенный dict; у OpenAI-формата
аргументы приезжают строкой JSON — разбирает адаптер, наружу строка не выходит).

**`ToolSpec`** — `name`, `description`, `input_schema` (JSON Schema аргументов). Имя поля
совпадает с полем нативного Anthropic API (`tools[].input_schema`) — причина именно в
этом, а не в каком-либо правиле репозитория; для OpenAI-формата адаптер кладёт ту же
схему в `function.parameters`. Конструктор `ToolSpec.from_model(name, description, ArgsModel)`
делает схему из Pydantic-модели аргументов — этим будет пользоваться реестр инструментов
(OVE-21), отдавая наружу готовый список `ToolSpec`; сам реестр живёт в `libs/tools`, в
`libs/llm` его нет.

**`LLMResponse`** — `model`, `stop_reason`, `text`, `tool_calls`, `usage`, `raw`. Два
исхода различаются явно: `stop_reason="tool_use"` и непустой `tool_calls` (есть свойство
`has_tool_calls`) — модель просит вызвать инструменты; `stop_reason="end_turn"` — финальный
текст. `StopReason` — общий для провайдеров литерал `end_turn` / `tool_use` /
`max_tokens` / `stop_sequence` / `content_filter`, в него адаптер укладывает и `finish_reason`
OpenAI, и `stop_reason` Anthropic. `usage` (входные и выходные токены) заполняется для
аудита и стоимости, `raw` — сырой ответ провайдера для отладки. `to_message()` собирает
из ответа `assistant`-сообщение для дописывания в историю вместе с `tool_calls` —
цикл агента не должен делать это руками и терять вызовы.

**`LLMClient.complete()`** — единственный метод контракта:

```python
async def complete(
    self,
    messages: Sequence[ChatMessage],
    *,
    tools: Sequence[ToolSpec] | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float | None = None,
) -> LLMResponse: ...
```

`model=None` — берётся `default_model` клиента. Остальные параметры keyword-only намеренно:
добавить следующий (`tool_choice`, `stop_sequences`) можно, не ломая вызовы. Плюс
неабстрактный `aclose()` для закрытия HTTP-клиента.

**Стриминга в v1 нет и метода `stream()` в контракте нет.** Не «отложенная заглушка», а
осознанное отсутствие: абстрактный метод заставил бы каждого клиента писать мёртвый
override, а токены всё равно некуда стримить — WebSocket-протокол ещё не зафиксирован (см.
ниже), и первый рабочий цикл «запрос → инструмент → ответ» синхронного `complete()`
полностью закрывает. Стриминг приедет отдельной задачей вместе с форматом WS-сообщений и
добавит `stream()` рядом с `complete()`, не меняя типы.

Реализации клиентов (`AnthropicClient`, `DeepSeekClient`) пока заготовки: конструкторы
проверяют ключ, `complete()` кидает `NotImplementedError` — это следующие задачи.

`libs/llm/factory.py` — `get_llm_client(settings=None)` выбирает клиента по
`settings.llm_provider` и кэширует по одному инстансу на провайдера на процесс (OVE-8:
«one active provider per running instance is enough»), а не создаёт новый при каждом
вызове — иначе `DeepSeekClient` плодил бы новые `httpx.AsyncClient` с утечкой соединений.
Фабрика клиента не закрывает: тот, кто подключит `get_llm_client()` к lifespan `apps/api`
(OVE-16), отвечает за вызов `aclose()` на выключении.

`libs/tools` пуст. Инструменты, требующие Windows/COM/Playwright, будут исполняться в
`apps/executor` — сюда попадёт только их описание и прокси-вызов.

### ORM-модели `libs/db/models`

`Conversation` (`conversations`) и `Message` (`messages`, OVE-11) — первые ORM-модели в
проекте. `Message.tool_calls` хранит те же `ToolCall` из `libs/llm/base.py`, а не
параллельную jsonb-схему: (де)сериализацию делает `ToolCallListType` — `TypeDecorator` рядом
с моделью, в `libs/db/models/message.py`, через `model_dump(mode="json")` / `model_validate`
Pydantic-модели `ToolCall`. `impl` этого типа — `JSONB(none_as_null=True)`: без этого флага
Python `None` лёг бы в колонку как JSON-литерал `null` внутри значения, а не как настоящий
SQL `NULL`.

`Message.tool_call_id` и `Message.is_error` зеркалят одноимённые поля `ChatMessage` — без
них `role="tool"`-сообщение нельзя восстановить в валидный `ChatMessage` при чтении из базы.
Форму `tool_call_id` (обязателен у `role="tool"`, `NULL` у остальных ролей) дублирует на
уровне БД `CheckConstraint` `tool_role_requires_tool_call_id`, зеркальный
`model_validator`'у `ChatMessage._check_role_shape`. `is_error` в контракте `ChatMessage` —
не опциональное поле, а `bool` с дефолтом `False` для любой роли, но `_check_role_shape`
всё же частично ограничивает его ролью: `is_error=True` допустим только у `role="tool"`,
а `is_error=False` (в том числе дефолт) валиден при любой роли. В БД это зеркалит второй
`CheckConstraint`, `non_tool_role_forbids_is_error` (`role = 'tool' OR is_error = false`),
рядом с `tool_role_requires_tool_call_id`, а не вместо него; `NOT NULL DEFAULT false`
остаётся.

Порядок `Conversation.messages` держит не `created_at` (при близких по времени вставках —
например, tool-call и его результат в одном ходе диспетчера — порядок по времени не
гарантирован), а `Message.sequence`: `BigInteger` с `Identity()`, монотонно растущий на
уровне самой Postgres. `created_at` остаётся для отображения и аудита, но не как
сортировочный ключ — порядок сообщений напрямую определяет, что уйдёт в LLM как история.

## Хранилища

**PostgreSQL 16** (SQLAlchemy 2.x async + asyncpg) — история диалогов, память агента, логи
вызовов инструментов (аудит: что вызвали, с какими аргументами, что вернулось). Первые
ORM-модели — `Conversation` и `Message`, см. раздел «ORM-модели `libs/db/models`» выше.

**Redis 7** — очередь Arq, pub/sub событий (в том числе канал до `executor`), кэш контекста,
rate limiting.

## Миграции

Alembic настроен на async engine и `libs.db` (`alembic/env.py`).

```bash
uv run alembic revision --autogenerate -m "описание"
uv run alembic upgrade head
uv run alembic downgrade -1
```

Новые модели обязательно импортируются в `libs/db/models/__init__.py`, иначе
`--autogenerate` их не увидит. Модель и миграция едут одним коммитом — см.
[CONTRIBUTING.md](../../CONTRIBUTING.md).

## Что ещё не специфицировано

**Точный контракт tool-calling схем** — какие именно инструменты существуют, как называются,
какие у них JSON-схемы аргументов и что они возвращают — не описан. Когда контракт
появится, он станет отдельным файлом в `.claude/knowledge/` (по образцу `api-contract.md`),
и его нужно будет добавить в таблицу и в блок `@`-импортов корневого `CLAUDE.md`. До тех
пор не считай имена инструментов и формы их аргументов зафиксированными и не выдумывай их
задним числом — спрашивай.

Так же не зафиксированы: формат сообщений WebSocket-протокола (сейчас эхо-заглушка),
протокол между `worker` и `executor`, схема хранения памяти агента.
