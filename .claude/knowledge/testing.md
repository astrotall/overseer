# Тесты и проверки качества

## Текущее состояние

Тестовая инфраструктура настроена и работает:

- `tests/conftest.py` — общие фикстуры (см. ниже);
- `tests/integration/test_health.py` — smoke-тест `GET /health`;
- `tests/unit/` — юнит-тесты бизнес-логики: конфиг, LLM-клиенты и фабрика, контракт
  `libs/llm/base.py`, протокол инструмента `libs/tools/base.py` (`test_tool_protocol.py`:
  прямой вызов `EchoTool`, построение `ToolSpec`, ошибки аргументов и исключение внутри
  инструмента как `ToolResult`, а также разделение трёх исходов по логам — трейсбек на баге
  инструмента, его отсутствие на плохих аргументах и проброс `RUNTIME_FAILURES` наружу; логи
  читаются через `structlog.testing.capture_logs([format_exc_info])`, то есть проверяется
  отрендеренный трейсбек, а не флаг `exc_info`), ORM-модель `Message`, системный промпт,
  а по `apps/voice` — конфиг,
  wake word и листенер, эндпоинтинг (`vad.py`), фильтры транскрипта (`stt.py`),
  оркестрация (`pipeline.py`), нарезка текста под синтез (`tts.py`), воспроизведение через
  фейковый `AudioSink` (`playback.py`), переходы состояния на озвучке
  (`test_voice_speaker.py`), клиент `/ws/chat` через фейковое соединение
  (`test_voice_ws_client.py`) и изоляция от CI (`test_voice_ci_isolation.py`);
- секции `[tool.pytest.ini_options]` и `[tool.coverage.*]` в `pyproject.toml`;
- `.pre-commit-config.yaml` — хуки на трёх стадиях (`pre-commit`, `commit-msg`, `pre-push`);
- `.github/workflows/ci.yml` — CI на GitHub Actions.

pre-commit и CI реально работают: на них можно ссылаться как на действующие проверки.

## Что установлено

Dev-зависимости из `pyproject.toml` (группа `dev`): `pytest>=8.3`, `pytest-asyncio>=0.24`,
`pytest-cov>=6.0`, `httpx>=0.27`, `ruff>=0.8`, `mypy>=1.13`. Пакетный менеджер — `uv`,
всё запускается через `uv run`.

Отдельно живёт группа `voice` (`openwakeword`, `sounddevice`, `faster-whisper`, `torch`):
она ставится только на машине, где реально слушают микрофон и говорят в динамик, —
`uv sync --group voice`. Ни CI, ни образы `api` и `worker` её не ставят, поэтому тесты не
имеют права импортировать `apps/voice/capture.py` и `apps/voice/main.py` (там `sounddevice`
на верхнем уровне) и создавать `OpenWakeWordDetector`, `FasterWhisperSTT`, `BeepCue`,
`SileroTTS` или `SoundDeviceSink` — эти пять классов импортируют свой движок лениво, внутри
`__init__`. Всё остальное в `apps/voice` — `audio.py`, `state.py`, `listener.py`,
`config.py`, `vad.py`, `pipeline.py`, `tts.py`, `playback.py`, `ws_client.py` и порты
`WakeWordDetector` / `SpeechToText` / `Cue` / `TextToSpeech` / `AudioSink` — импортируется
без звуковой карты и покрывается юнит-тестами через фейки. `ws_client.py` в этом списке
особый случай: он вообще не тянет группу `voice` — `websockets` лежит в основных
зависимостях, — а `WSConnection` и `Connector` сделаны портами, чтобы тесты гоняли клиент
целиком (отправка, приём, дроп по epoch, реконнект с backoff, гейт готовности соединения) без
сокета. `playback.py` в этом списке новичок: до OVE-47 он был
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
- зарегистрированные маркеры: `integration` (требует живых PostgreSQL/Redis) и `windows`
  (требует Windows и live-сессии пользователя, `apps/executor`).

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

1. `astral-sh/setup-uv` с кэшем, `uv sync --dev`;
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
