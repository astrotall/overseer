# Тесты и проверки качества

## Текущее состояние

Тестовая инфраструктура настроена и работает:

- `tests/conftest.py` — общие фикстуры (см. ниже);
- `tests/integration/test_health.py` — smoke-тест `GET /health`;
- `tests/integration/test_chat_service.py` — ход диалога целиком на живой базе: цикл вызова
  инструментов (OVE-22) — выполнение, персист `tool_use` + `tool_result` + финального
  текста, лимит раундов, инструмент с `requires_confirmation` и подменяемый шов
  подтверждения, выдуманное моделью имя инструмента, откат хода при падении LLM посреди
  цикла, — и два параметризованных теста среза истории: при любом `history_limit` то, что
  уходит в LLM, начинается с `role="user"`, не содержит `tool_result` без своего `tool_use`
  и `tool_use` без своего `tool_result`. Второй из них
  (`test_history_slice_never_starts_mid_turn_around_an_exhausted_turn`) отличается тестовыми
  данными: в середине диалога стоит ход, который кончился **по лимиту раундов**, а не
  ответом модели, — записанный не руками, а прогоном самой `ChatService` с
  `max_tool_rounds=2`. Это единственная концовка хода, форма которой держится на явном
  решении *не сохранять* последний невыполненный `tool_use` (OVE-22), поэтому она проверена
  отдельно, а не считается корректной по построению; парность вызовов и результатов
  пришпилена в общем хелпере `_assert_history_is_well_formed`, и без этого решения тест
  краснеет на срезах, задевающих такой ход;
- `tests/integration/test_chat_confirmation.py` — пауза хода на инструменте с
  `requires_confirmation=True` (OVE-26) на живых PostgreSQL и Redis: инструмент не
  исполняется, `PendingConfirmation` создаётся с тем же `tool_call_id`, что у модели, а в
  базе после паузы остаётся только сообщение пользователя — и, если раунд инструментов до
  паузы успел закончиться, его `tool_use` и `tool_result` целой парой. Там же — правило «раунд
  решается целиком до первого `execute()`»: раунд из двух вызовов, где второй требует
  подтверждения, не исполняет и первый (счётчик исполнений `CountingEchoTool` остаётся нулём),
  раунд без подтверждений исполняет все вызовы как раньше, а раунд из двух
  confirmation-required вызовов заводит `PendingConfirmation` только на первый — известное
  ограничение v1, пришпиленное тестом, а не оставленное на честное слово. Форму ответа обоих
  транспортов на ту же паузу (`202` с `confirmation_id` и `summary` у REST, конверт
  `confirmation_required` у WS) проверяют `test_conversations_api.py` и `test_ws_chat.py`
  со стором-двойником без Redis: там проверяется транспорт, а не хранилище;
- `tests/integration/test_confirmation_resume.py` — возобновление хода (OVE-27) на живых
  PostgreSQL и Redis: подтверждение исполняет инструмент и доводит ход до ответа;
  продолжение может само позвать ещё один инструмент и может упереться в **ещё одну** паузу
  (возобновление не одноразовое, первый pending при этом уже снят); отказ не исполняет
  инструмент вовсе и уезжает модели как `tool_result` с `is_error=True`; повторное
  разрешение того же `confirmation_id` и неизвестный id — `NotFoundError`, а не тихий no-op.
  Там же — параметризованный по `history_limit` тест среза истории **у настоящего
  потребителя**: то, что уходит в LLM на продолжении, начинается с `role="user"` и не
  содержит непарных `tool_use` / `tool_result`. До OVE-27 это поведение
  `cut_to_turn_boundary()` на возобновлённом ходе только предполагалось. Там же —
  **настоящая гонка, а не её последовательная имитация**: два `confirm()` на один
  `confirmation_id` через `asyncio.gather` на независимых `AsyncSession` (тем же приёмом, что
  гонка `get_or_create_default_conversation` в OVE-13, включая ручную уборку за собой —
  фикстуры `db_session` с откатом здесь нет). Инструмент обязан выполниться **ровно один
  раз**: один вызов возвращает ответ ассистента, второй — `ConflictError`. Тест краснеет,
  если вернуть `get_pending()` на место `claim_pending()`, — то есть охраняет именно захват,
  а не общее «ничего не упало»;
- `tests/integration/test_confirmation_store.py` — сам стор на живом Redis: круговой путь
  записи, TTL, `NotFoundError` на неизвестном id и захват (OVE-27) — восемь одновременных
  `claim_pending()` дают ровно один успех и семь `ConflictError`, а захват переживает снятую
  им запись (после `resolve_pending()` тот же id — `NotFoundError`, а не `ConflictError`).
  Там же — атомарность самого захвата: запись, исчезнувшая перед вызовом, даёт `NotFoundError`
  и **не оставляет ключа-захвата**, а `test_the_claim_reads_and_locks_inside_one_redis_command`
  следит за командами, которые клиент шлёт во время `claim_pending()`: `GET` и `SET` там
  запрещены, разрешён только `EVALSHA`. Второй тест — единственный, который краснеет на
  возврате к двум последовательным командам: гонку «запись истекла ровно между чтением и
  захватом» по времени не воспроизвести надёжно, поэтому проверяется механизм, а не удача;
- `tests/integration/test_confirmations_api.py` — те же исходы через HTTP:
  `POST /confirmations/{id}/confirm` и `/reject` отдают обычный `MessageResponse` (200),
  неизвестный или уже разрешённый id — 404, захваченный другим запросом — 409, а продолжение,
  упёршееся в новое подтверждение, — 202 с новым `confirmation_id`. Каждый тест здесь
  подменяет `get_active_llm_client`, даже тот, что проверяет 404 и до LLM не доходит:
  зависимости маршрута FastAPI резолвит **до** тела хендлера, поэтому без подмены тест падает
  в CI на `ConfigurationError` (ключа провайдера там нет), а не на проверяемом коде;
- `tests/integration/test_browser_playwright.py` — жизненный цикл браузера (OVE-36) на
  **живом Chromium**: контекст умеет отрисовать страницу, куки одного разговора переживают
  вызовы и не видны другому разговору, истёкший по простою контекст действительно закрыт (и
  новый приезжает чистым), а `aclose()` не оставляет за собой ни браузера, ни открытых
  страниц. Помечен `@pytest.mark.browser`, а не `integration`: PostgreSQL и Redis ему не
  нужны, нужен движок из группы `browser`. Часы подменены — простой в тесте проматывается,
  а не выжидается. Без установленного Chromium тест пропускается, но под `CI` поднимает
  исходную ошибку: там движок ставится шагом пайплайна, и молча зелёный прогон означал бы,
  что жизненный цикл никто не проверил. В CI эти тесты идут с `BROWSER_NO_SANDBOX=true`
  (Ubuntu 24.04 запрещает непривилегированные user namespace'ы), а песочница проверяется
  отдельно — прогоном этого же файла внутри собранного образа `apps/api`:

  ```bash
  docker compose -f docker/docker-compose.yml build api
  # в образе нет dev-группы, поэтому pytest доставляется одноразовым слоем поверх него
  printf 'FROM overseer-api\nUSER root\nRUN uv sync --frozen --group browser\nUSER overseer\n' \
      | docker build -t overseer-api-test -f - .
  docker run --rm --security-opt seccomp=./docker/chromium-seccomp.json \
      -v "$PWD/tests:/app/tests:ro" -v "$PWD/pyproject.toml:/app/pyproject.toml:ro" \
      overseer-api-test pytest -m browser
  ```

  Разбор, что именно этим проверяется и почему без seccomp-профиля Chromium не стартует, —
  в [architecture.md](architecture.md), раздел «Песочница Chromium»;
- `tests/unit/` — юнит-тесты бизнес-логики: конфиг, LLM-клиенты и фабрика, контракт
  `libs/llm/base.py`, протокол инструмента `libs/tools/base.py` (`test_tool_protocol.py`:
  прямой вызов `EchoTool`, построение `ToolSpec`, ошибки аргументов и исключение внутри
  инструмента как `ToolResult`, а также разделение трёх исходов по логам — трейсбек на баге
  инструмента, его отсутствие на плохих аргументах и проброс `RUNTIME_FAILURES` наружу; логи
  читаются через `structlog.testing.capture_logs([format_exc_info])`, то есть проверяется
  отрендеренный трейсбек, а не флаг `exc_info`. Отдельно пришпилена асимметрия «наружу —
  фиксированная фраза, в лог — всё»: сообщение исключения с путём и именем файла попадает в
  трейсбек, но не в `ToolResult`, и текст результата одинаков для любого исключения этой
  ветки), ORM-модель `Message`, системный промпт,
  а по `apps/voice` — конфиг,
  wake word и листенер, эндпоинтинг (`vad.py`), фильтры транскрипта (`stt.py`),
  оркестрация (`pipeline.py`), нарезка текста под синтез (`tts.py`), воспроизведение через
  фейковый `AudioSink` (`playback.py`), переходы состояния на озвучке
  (`test_voice_speaker.py`), клиент `/ws/chat` через фейковое соединение
  (`test_voice_ws_client.py`), голосовое подтверждение (`test_voice_confirmations.py` —
  классификация «да»/«нет» по тексту и `HTTPConfirmationAPI` на `httpx.MockTransport`:
  200, 202, 404/409, сетевой сбой и тело неожиданной формы; плюс класс `TestConfirmation`
  в `test_voice_ws_client.py` — весь диалог целиком на фейках, включая то, что ответ
  пользователя **не уезжает в чат сообщением**, что вопрос звучит раньше, чем клиент
  начинает слушать, переспрос и откат к отказу, молчание по таймауту, недоступный API и
  ответ, записанный до реконнекта; там же — цепочка подтверждений: последнее звено, которое
  разрешилось, кончается ответом ассистента и **молчит** про лимит, а предупреждение звучит
  только у цепочки, которая упёрлась в лимит с неразрешённым подтверждением; программный
  вход в запись — в `test_voice_wake_word.py`, там же, где обычный путь через wake word:
  те же инварианты
  `generation`, гейта соединения и зафиксированной эпохи) и изоляция от CI
  (`test_voice_ci_isolation.py`). Сверку `generation` **в точке использования результата**
  держат три теста-гонки, по одному на каждую точку: реплика, дописанная после того, как ход
  отобрали (эндпоинтер меняет состояние ровно между концом реплики и `_emit`,
  `test_voice_wake_word.py`), транскрипт, чей ход сменился **пока шло распознавание** (STT
  меняет состояние изнутри `transcribe`, `test_voice_pipeline.py`), и ответ, записанный в
  прошлую попытку подтверждения и доехавший в следующую (`test_voice_ws_client.py`).
  У последнего `epoch` совпадает — значит тест краснеет ровно на снятой проверке
  `generation`, а не на чём-то ещё. Там же, рядом с ними, зафиксировано и то, что дроп чужого
  хода **не возвращает состояние в `idle`**: ход остаётся у нового владельца. А по
  `apps/api` — подрезка среза истории до границы хода (`test_chat_history_slice.py`:
  чистая функция `cut_to_turn_boundary`, без базы). Там же — `test_browser_sessions.py`:
  вся логика `BrowserSessionManager` (OVE-36) на фейковом бэкенде и управляемых часах —
  переиспользование контекста разговором и изоляция разговоров, ленивый запуск браузера при
  первом вызове, закрытие по простою и то, что **занятый вызовом контекст не закрывается
  из-под него**, остановка браузера на последнем истёкшем контексте и его самостоятельный
  подъём на следующем вызове, вытеснение самой давней незанятой сессии на потолке и
  неприкосновенность занятой, живучесть сборщика после сбоя. Ни одного запущенного браузера
  здесь нет: движок за портом `BrowserBackend`, поэтому эти тесты идут в CI и без группы
  `browser`. Там же — восстановление после падения браузера **между двумя арендами одного
  разговора**: фейковый бэкенд «роняет» браузер (`crash()` гасит `connected`, оставляя
  `running`), и следующая аренда обязана выдать новый контекст, а не сохранённый мёртвый.
  Тест краснеет ровно на снятой проверке `connected` в `_lease()` — до OVE-36 повторная
  аренда смотрела только на наличие записи в таблице сессий;
- секции `[tool.pytest.ini_options]` и `[tool.coverage.*]` в `pyproject.toml`;
- `.pre-commit-config.yaml` — хуки на трёх стадиях (`pre-commit`, `commit-msg`, `pre-push`);
- `.github/workflows/ci.yml` — CI на GitHub Actions.

pre-commit и CI реально работают: на них можно ссылаться как на действующие проверки.

## Что установлено

Dev-зависимости из `pyproject.toml` (группа `dev`): `pytest>=8.3`, `pytest-asyncio>=0.24`,
`pytest-cov>=6.0`, `httpx>=0.27`, `ruff>=0.8`, `mypy>=1.13`. Пакетный менеджер — `uv`,
всё запускается через `uv run`.

Отдельно живёт группа `browser` (`playwright`): она нужна там, где реально запускают
Chromium, — в образе `apps/api` (`docker/Dockerfile.api` ставит и группу, и сам движок
через `playwright install --with-deps chromium`) и в CI. Локально —
`uv sync --group browser && uv run playwright install chromium`. Образ `apps/worker` её не
ставит: браузером он не пользуется. На тесты это влияет ровно одним способом: без группы
пропускается `tests/integration/test_browser_playwright.py`, а юнит-тесты менеджера сессий
работают в любом окружении — `libs/browser/session.py` про Playwright ничего не знает, а
`libs/browser/backend.py` импортирует его лениво, при первом запуске браузера.

Отдельно живёт группа `voice` (`openwakeword`, `sounddevice`, `faster-whisper`, `torch`):
она ставится только на машине, где реально слушают микрофон и говорят в динамик, —
`uv sync --group voice`. Ни CI, ни образы `api` и `worker` её не ставят, поэтому тесты не
имеют права импортировать `apps/voice/capture.py` и `apps/voice/main.py` (там `sounddevice`
на верхнем уровне) и создавать `OpenWakeWordDetector`, `FasterWhisperSTT`, `BeepCue`,
`SileroTTS` или `SoundDeviceSink` — эти пять классов импортируют свой движок лениво, внутри
`__init__`. Всё остальное в `apps/voice` — `audio.py`, `state.py`, `listener.py`,
`config.py`, `vad.py`, `pipeline.py`, `tts.py`, `playback.py`, `ws_client.py`,
`confirmations.py` и порты `WakeWordDetector` / `SpeechToText` / `Cue` / `TextToSpeech` /
`AudioSink` — импортируется
без звуковой карты и покрывается юнит-тестами через фейки. `ws_client.py` в этом списке
особый случай: он вообще не тянет группу `voice` — `websockets` лежит в основных
зависимостях, — а `WSConnection` и `Connector` сделаны портами, чтобы тесты гоняли клиент
целиком (отправка, приём, дроп по epoch, реконнект с backoff, гейт готовности соединения) без
сокета. `confirmations.py` (OVE-50) — тот же случай и по той же причине: `httpx` лежит в
основных зависимостях, а `httpx.MockTransport` закрывает все коды ответа REST-эндпоинта
OVE-27 без поднятого `apps/api`. `playback.py` в этом списке новичок: до OVE-47 он был
не написан, а в задел OVE-44 попал как модуль с `sounddevice` на верхнем уровне; порт
`AudioSink` сделал его CI-safe, и не-CI-safe остался один `capture.py`.

Это правило не на честном слове: `tests/unit/test_voice_ci_isolation.py` поднимает
подпроцесс с блокирующим `sys.meta_path`-финдером и убеждается, что каждый CI-safe модуль
`apps/voice` импортируется, ни разу не тронув `sounddevice`, `openwakeword`,
`faster_whisper`, `ctranslate2` или `torch`. Добавили в такой модуль импорт движка на верхнем
уровне — тест покраснеет здесь, а не в CI на `ubuntu-latest`.

## Команды

```bash
uv sync                       # установить зависимости, включая dev-группу

uv run pytest                 # весь набор
uv run pytest tests/unit      # только юнит-тесты
uv run pytest -m integration  # только интеграционные
uv run pytest --cov --cov-report=term-missing   # с покрытием

uv run ruff check .           # линтер
uv run ruff format .          # форматирование
uv run mypy apps libs         # типы

uv run alembic upgrade head   # миграции должны накатываться на пустую базу

pre-commit run --all-files    # прогнать хуки по всему дереву
```

Инфраструктура для интеграционных тестов:

```bash
docker compose -f docker/docker-compose.yml up -d postgres redis
```

## Настройки pytest

Из `[tool.pytest.ini_options]`:

- `testpaths = ["tests"]`, `pythonpath = ["."]` — импорты `apps.*` / `libs.*` работают без
  установки пакета;
- `addopts = "-ra --strict-markers --strict-config"`. `--strict-markers` означает, что
  **незарегистрированный маркер — ошибка, а не предупреждение**: новый маркер сначала
  добавляется в `markers` в `pyproject.toml`, потом используется в коде;
- `asyncio_mode = "auto"` — async-тесты пишутся без декоратора `@pytest.mark.asyncio`;
- `asyncio_default_fixture_loop_scope` и `asyncio_default_test_loop_scope` — оба `session`.
  Один event loop на весь прогон, поэтому сессионные async-фикстуры (`db_engine`) переживают
  отдельные тесты. У pytest-asyncio 1.x фикстуры `event_loop` больше нет — область цикла
  задаётся только этими двумя настройками;
- зарегистрированные маркеры: `integration` (требует живых PostgreSQL/Redis), `browser`
  (требует Playwright с Chromium из группы `browser`) и `windows` (требует Windows и
  live-сессии пользователя, `apps/executor`).

## Покрытие

`[tool.coverage.run]`: `source = ["apps", "libs"]`, `branch = true`,
`omit` — `__pycache__` и `apps/executor/*` (его нельзя выполнить вне Windows, см. ниже).
`[tool.coverage.report]`: `show_missing`, `skip_covered`, и `exclude_lines` для
`pragma: no cover`, `if TYPE_CHECKING:`, `raise NotImplementedError` и `...` (тела
абстрактных методов в `libs/llm/base.py`).

Порога `fail_under` нет: покрытие — информация, а не гейт. CI считает его и складывает
`coverage.xml` в артефакты, но упавшим прогон от низкого покрытия не станет.

## База данных для тестов

Тесты работают на **отдельной базе**. Адрес берётся из `Settings.database_url_test`
(переменная `DATABASE_URL_TEST`); если она не задана, `test_database_url` выводит адрес из
`database_url`, подставляя суффикс `_test` в имя базы. Писать в рабочую базу тесты не могут
физически.

Базу нужно создать один раз:

```bash
docker compose -f docker/docker-compose.yml exec postgres createdb -U overseer overseer_test
```

Если база недоступна, фикстура `db_engine` **пропускает** зависящие от неё тесты с понятным
сообщением — локально юнит-тесты гоняются без поднятой инфраструктуры. Исключение —
переменная окружения `CI`: там недоступная база поднимает исходную ошибку, потому что молча
зелёный прогон без реальной проверки хуже красного.

Почему отдельная база, а не testcontainers — обоснование в
[README.md](../../README.md), раздел «База данных для тестов»; не дублируй его здесь.

## Фикстуры из `tests/conftest.py`

| Фикстура | Область | Что даёт |
|---|---|---|
| `anyio_backend` | session | `"asyncio"` для тестов под `@pytest.mark.anyio` |
| `settings` | session | `get_settings()` |
| `test_database_url` | session | адрес тестовой БД (см. выше) |
| `db_engine` | session | async engine на тестовой базе, `NullPool`; схема создаётся один раз через `Base.metadata.create_all` |
| `db_session` | function | `AsyncSession` внутри внешней транзакции с `join_transaction_mode="create_savepoint"`; после теста всё откатывается, тесты не видят данных друг друга |
| `app` | function | экземпляр FastAPI из `apps.api.main`; `dependency_overrides` сбрасываются после теста |
| `async_client` | function | `httpx.AsyncClient` поверх `ASGITransport` |

`ASGITransport` **не запускает `lifespan`**, поэтому в тестах не поднимаются подключения к
PostgreSQL и Redis из `apps/api/main.py`. Роутам, которым нужна БД, подменяй зависимость:
`app.dependency_overrides[get_session]` + фикстура `db_session`.

## Как раскладывать тесты

- `tests/unit/` — без внешних зависимостей: чистая логика, схемы, хелперы. Быстрые, не ходят
  в сеть и в БД.
- `tests/integration/` — с живыми PostgreSQL и Redis: репозитории, эндпоинты, Arq-задачи.
  Помечаются `@pytest.mark.integration`.
- Общие фикстуры — в `tests/conftest.py`, специфичные для подкаталога — в его собственном
  `conftest.py` (пока такого нет).
- Тесты, которым нужен живой Chromium, помечаются `@pytest.mark.browser` и лежат в
  `tests/integration/`: маркер `integration` в проекте означает живые PostgreSQL и Redis, а
  браузеру нужен другой внешний движок — смешивать их в одном маркере значило бы требовать
  базу там, где её нет.
- `apps/executor` тестируется только на Windows: COM/win32com и Playwright с живым профилем
  не работают в контейнере и в CI на Linux. Такие тесты помечаются `@pytest.mark.windows` и
  пропускаются вне Windows — падать в общем прогоне они не должны.

## Настройки линтера и типов

Из `pyproject.toml`, менять только осознанно:

- **ruff**: `line-length = 100`, `target-version = "py311"`,
  `extend-exclude = [".venv", "alembic/versions"]` (автогенерённые миграции не линтуются);
  правила `E, F, I, UP, B, ASYNC, C4, SIM, PT`. `PT` — flake8-pytest-style, с
  `fixture-parentheses = false`: фикстуры объявляются как `@pytest.fixture`, без скобок.
  isort знает `apps` и `libs` как first-party. Форматтер — с `docstring-code-format`.
- **mypy**: `python_version = "3.11"`, плагин `pydantic.mypy`,
  `disallow_untyped_defs = true` (аннотации обязательны у всех функций),
  `warn_unused_ignores = true` (лишний `# type: ignore` — ошибка),
  `warn_redundant_casts`, `warn_unused_configs`, `ignore_missing_imports = true`,
  `exclude` — `.venv`, `build`, `dist`.

`disallow_untyped_defs` — причина, по которой новый код без аннотаций просто не пройдёт
проверку; это не стилевое пожелание.

Хуки и CI зовут mypy как `mypy apps libs`, поэтому `tests/` и `alembic/` типами
**не проверяются** — хотя конфиг их не исключает и `uv run mypy tests` отработает вручную.

## Git-хуки (pre-commit)

Конфиг — [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml),
`default_install_hook_types: [pre-commit, commit-msg, pre-push]`. Установка описана в
[CONTRIBUTING.md](../../CONTRIBUTING.md). Локальные хуки — `language: system`: ruff, mypy и
pytest гоняются через `uv run`, теми же версиями, что зафиксированы в `uv.lock`, отдельного
окружения pre-commit для них не создаётся. Единственный внешний хук —
`conventional-pre-commit` (репозиторий `compilerla/conventional-pre-commit`, `rev: v3.6.0`),
он живёт в своём окружении; список допустимых типов коммита передаётся ему аргументами и
должен совпадать с таблицей типов в CONTRIBUTING.md.

| Стадия | Хук | Что делает |
|---|---|---|
| `pre-commit` | `ruff-check` | `ruff check --fix --force-exclude` по staged `*.py` |
| `pre-commit` | `ruff-format` | `ruff format --force-exclude` по staged `*.py` |
| `pre-commit` | `mypy` | `mypy apps libs` — `pass_filenames: false`, то есть по всему дереву целиком, но только если в коммите есть `*.py` |
| `commit-msg` | `conventional-pre-commit` | формат Conventional Commits **жёстко**: сообщение не того вида коммит отклоняет |
| `commit-msg` | `task-key` | `scripts/check_task_key.py` — ищет `OVE-<n>`, **только предупреждение**, коммит не отменяет; для сообщений, начинающихся с `chore`/`ci`/`build`/`revert`/`merge`/`bump`, молчит вовсе |
| `pre-push` | `pytest` | `uv run pytest` — весь набор, `always_run` |

Коммит, не задевший `*.py`, три первых хука пропускает. Обход — `--no-verify` (`-n`).

## Что проверяет CI

[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) — на каждый pull request в
`main` и на push в `main`, один job `checks` на `ubuntu-latest`, с
`concurrency: cancel-in-progress`. Сервис-контейнеры `postgres:16-alpine` и `redis:7-alpine`
(оба с healthcheck) публикуются на стандартных `5432` / `6379` — не на локальных `55432` /
`56379` из `.env.example`; адреса задаются переменными job: `DATABASE_URL`,
`DATABASE_URL_TEST`, `REDIS_URL`.

Шаги по порядку:

1. `astral-sh/setup-uv` с кэшем, `uv sync --dev --group browser` и
   `uv run playwright install --with-deps chromium` — Chromium ставится в CI, потому что
   тесты жизненного цикла браузера гоняются на настоящем движке, а не на фейке;
2. `uv run ruff check .` — линт;
3. `uv run ruff format --check .` — форматирование;
4. `uv run mypy apps libs` — типы;
5. `uv run alembic upgrade head` — миграции обязаны накатываться на чистую базу. Это ловит
   самую частую поломку: модель добавили, миграцию забыли;
6. `psql ... -c 'CREATE DATABASE overseer_test'` — тестовая база;
7. `uv run pytest --cov --cov-report=term-missing --cov-report=xml`;
8. загрузка `coverage.xml` артефактом (`if: always()`, `if-no-files-found: ignore`).

Падение любого шага роняет джобу. Красный CI не мержим.

Поверх CI на PR'ы приезжает CodeRabbit — внешнее advisory-ревью, мерж оно не блокирует;
подробности в [CONTRIBUTING.md](../../CONTRIBUTING.md).
