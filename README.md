# Overseer

<div align="center">

![Python](https://img.shields.io/badge/-Python_3.11-3776AB?logo=python&logoColor=white&style=for-the-badge)
![FastAPI](https://img.shields.io/badge/-FastAPI-009688?logo=fastapi&logoColor=white&style=for-the-badge)
![Pydantic](https://img.shields.io/badge/-Pydantic_v2-E92063?logo=pydantic&logoColor=white&style=for-the-badge)
![PostgreSQL](https://img.shields.io/badge/-PostgreSQL-4169E1?logo=postgresql&logoColor=white&style=for-the-badge)
![SQLAlchemy](https://img.shields.io/badge/-SQLAlchemy_2.0-D71F00?logo=sqlalchemy&logoColor=white&style=for-the-badge)
![Alembic](https://img.shields.io/badge/-Alembic-6BA81E?style=for-the-badge)
![Redis](https://img.shields.io/badge/-Redis-FF4438?logo=redis&logoColor=white&style=for-the-badge)
![Arq](https://img.shields.io/badge/-Arq-1F6FEB?style=for-the-badge)
![Docker](https://img.shields.io/badge/-Docker-2496ED?logo=docker&logoColor=white&style=for-the-badge)
![uv](https://img.shields.io/badge/-uv-DE5FE9?style=for-the-badge)
![Ruff](https://img.shields.io/badge/-Ruff-D7FF64?logo=ruff&logoColor=black&style=for-the-badge)
![mypy](https://img.shields.io/badge/-mypy_strict-2A6DB2?style=for-the-badge)

</div>

[![CI](https://github.com/<owner>/overseer/actions/workflows/ci.yml/badge.svg)](https://github.com/<owner>/overseer/actions/workflows/ci.yml)

<!-- Replace <owner> with the real repository owner after the first push. -->

**Overseer** is a system-level AI agent that operates the user's computer from a
voice or text command.

## About the project

The idea is simple: the user says or types what needs to be done ("build the
August report from the export, put it in Word and send it by email"), and the
agent carries it out on their machine by driving real applications.

Key principles:

- **The LLM is the "brain".** No model of our own is trained or fine-tuned.
  We use ready-made providers (Anthropic Claude as the primary model, DeepSeek
  as the cheap one for simple/bulk requests). The model does not perform
  actions itself: through **function calling (tool use)** it decides *which
  tool to call and with which arguments*.
- **Applications are the "hands".** The agent's tools are wrappers around real
  software: **COM/win32com** (Word, Excel, Outlook and other Windows
  applications), **Playwright** (a browser with the user's profile), the file
  system, the shell.
- **Voice and text are the same input.** Speech is recognized and turned into
  the very same text request you would type on a keyboard.
- **Auditability.** Every tool call is a separate record: what was called, with
  which arguments, what came back.

## Architecture diagram

```
                 ┌───────────────────────────┐
  voice / text   │  Client (UI / CLI / mic)  │
   ────────────► │  REST + WebSocket stream  │
                 └─────────────┬─────────────┘
                               │
                    ┌──────────▼───────────┐        ┌──────────────────┐
                    │      apps/api        │◄──────►│    PostgreSQL    │
                    │ FastAPI: routes, WS, │        │ dialogs, tasks,  │
                    │ DI, DTO validation   │        │  call audit log  │
                    └───┬──────────────┬───┘        └──────────────────┘
                        │              │
            task enqueue│              │ tool calling
                        │              ▼
                 ┌──────▼──────┐   ┌──────────────────────────┐
                 │    Redis    │   │        libs/llm          │
                 │  Arq queue  │   │ Anthropic / DeepSeek     │
                 │   pub/sub   │   │ "brain": picks the tool  │
                 └──┬───────┬──┘   └──────────┬───────────────┘
                    │       │                 │
                    │       │                 ▼
        ┌───────────▼──┐    │        ┌────────────────────┐
        │ apps/worker  │    │        │     libs/tools     │
        │ Arq: long    │────┼───────►│  registry of the   │
        │ tasks, chains│    │        │  "hands": spec +   │
        │ of reasoning │    │        │  invocation        │
        └──────────────┘    │        └─────────┬──────────┘
                            │                  │
                            │ pub/sub          │ desktop commands
                            │                  ▼
                  ┌─────────▼──────────────────────────────┐
                  │           apps/executor                │
                  │  A SEPARATE process on Windows,        │
                  │  OUTSIDE Docker (a live user session   │
                  │  is required): COM/win32com,           │
                  │  Playwright, GUI                       │
                  └────────────────────────────────────────┘
```

Left to right, the flow is: request → `api` → the LLM decides which tool is
needed → the tool runs either directly in `worker` (if it is Python code: DB,
HTTP, files) or goes to `executor` over Redis (if Windows and a live desktop
are required) → the result is returned to the model → the model formulates the
answer → the answer is streamed to the client over WebSocket.

## Stack

| Layer | Technology |
|---|---|
| HTTP / WebSocket | FastAPI, Uvicorn |
| Validation, config | Pydantic v2, pydantic-settings |
| Database | PostgreSQL 16, SQLAlchemy 2.x (async), asyncpg |
| Migrations | Alembic (async engine) |
| Background tasks | Arq |
| Cache / queue / bus | Redis 7 |
| LLM | Anthropic Claude, DeepSeek |
| Logging | structlog |
| Package manager | uv |
| Infrastructure | Docker, docker compose |
| Code quality | ruff, mypy, pytest |

## Repository layout

The monorepo is split into **`apps/`** and **`libs/`**:

- **`apps/`** — things that *run*: processes with their own entry point and
  their own lifecycle. Each application is a separate container or service.
- **`libs/`** — things that are *reused*: shared code with no entry point.
  Libraries know nothing about their callers and never import `apps/`.
  The dependency is always one-directional: `apps/` → `libs/`.

```
overseer/
├── apps/
│   ├── api/                 # FastAPI: routes, WebSocket, DI dependencies
│   │   ├── main.py          #   app creation + lifespan (DB, Redis)
│   │   ├── deps.py          #   SessionDep / RedisDep / SettingsDep
│   │   └── routes/          #   health.py, ws.py
│   ├── worker/              # Arq worker: background and long-running tasks
│   └── executor/            # STUB: a separate Windows service outside Docker
│                            #   (COM/Playwright); see apps/executor/README.md
├── libs/
│   ├── core/                # config (pydantic-settings), logging (structlog), exceptions
│   ├── db/                  # SQLAlchemy Base, async engine/session, Redis, models
│   ├── llm/                 # Anthropic/DeepSeek clients, tool-calling base (stub)
│   ├── tools/               # the agent's tool registry (empty for now)
│   └── schemas/             # Pydantic v2 DTOs
├── alembic/                 # migrations, env.py wired to the async engine and libs.db
├── docker/
│   ├── docker-compose.yml   # postgres, redis, api, worker
│   ├── Dockerfile.api
│   └── Dockerfile.worker
├── tests/                   # conftest.py + unit/ and integration/
├── .env.example
└── pyproject.toml
```

## Running

### 1. With Docker (recommended)

```bash
cp .env.example .env          # fill in the LLM keys if needed
docker compose -f docker/docker-compose.yml up --build
```

This brings up `postgres:16`, `redis:7` (both with healthchecks), `api` and
`worker`. The API is available at <http://localhost:8000>, the docs at
<http://localhost:8000/docs>.

Check:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Migrations inside the container:

```bash
docker compose -f docker/docker-compose.yml exec api alembic upgrade head
```

### 2. Locally (no Docker for the application itself)

```bash
uv sync                                                   # install dependencies
docker compose -f docker/docker-compose.yml up -d postgres redis   # infrastructure only
cp .env.example .env

uv run alembic upgrade head                               # migrations
uv run uvicorn apps.api.main:app --reload                 # API
uv run arq apps.worker.main.WorkerSettings                # worker (in another terminal)
```

> **Ports.** By default the infrastructure is published to the host on `55432`
> (PostgreSQL) and `56379` (Redis), so it does not clash with services already
> running locally on the standard `5432` / `6379`. This is the default in all
> three places at once — in `Settings`, in `.env.example` and in the fallbacks
> of `docker-compose.yml` itself — so compose and the application agree on the
> same ports even without a copied `.env`. If those ports are free for you and
> you would rather have the usual ones, override `POSTGRES_PORT` / `REDIS_PORT`
> in `.env` (and the ports in `DATABASE_URL` / `DATABASE_URL_TEST` / `REDIS_URL`
> along with them). Inside the docker network the services always talk over the
> internal `5432` / `6379` — that does not need changing.

### Migrations

```bash
uv run alembic revision --autogenerate -m "description"
uv run alembic upgrade head
uv run alembic downgrade -1
```

Always import new models in `libs/db/models/__init__.py`, otherwise
`--autogenerate` will not see them.

## Testing

Tooling: **pytest** + **pytest-asyncio** (`asyncio_mode = "auto"`, so async tests are
written without decorators), **pytest-cov** for coverage and **httpx** with
`ASGITransport` for requests against the application without a real socket. All the
settings live in `pyproject.toml`, section `[tool.pytest.ini_options]`.

```bash
uv sync                       # dependencies, including the dev group

uv run pytest                 # the whole suite
uv run pytest tests/unit      # unit tests only, no infrastructure required
uv run pytest -m integration  # integration tests only
uv run pytest --cov --cov-report=term-missing   # with coverage
```

### Layout

- `tests/unit/` — pure logic, schemas, helpers. Fast, no network and no DB.
- `tests/integration/` — with live PostgreSQL and Redis: repositories, endpoints, Arq tasks.
- `tests/conftest.py` — shared fixtures.

Tests that need the infrastructure are marked `@pytest.mark.integration`; tests that
only work on Windows (`apps/executor`) are marked `@pytest.mark.windows`.

### The test database

Tests run against a **separate database**, whose address comes from `DATABASE_URL_TEST`.
If that variable is not set, the address is derived from `DATABASE_URL` by adding the
`_test` suffix to the database name — tests physically cannot write to the working
database.

```bash
docker compose -f docker/docker-compose.yml up -d postgres redis   # infrastructure
docker compose -f docker/docker-compose.yml exec postgres \
    createdb -U overseer overseer_test                             # once
```

If the database is unavailable, tests depending on the `db_session` fixture are
**skipped** with a clear message — locally you can run the unit tests without the
infrastructure up. In CI (the `CI` environment variable) an unavailable database is an
error: a silently green run without a real check is worse than a red one.

**Why a separate database and not testcontainers.** The development infrastructure is
brought up through `docker/docker-compose.yml` anyway, and in CI through GitHub Actions
`services:`. Testcontainers would add a dependency, require access to the docker socket
from inside the run and spin up a container on every pytest invocation — that is more
expensive and more fragile than a connection string to an already running postgres. This
decision is worth revisiting if we ever need to run the tests against several PostgreSQL
versions at once.

### Fixtures

| Fixture | What it gives |
|---|---|
| `anyio_backend` | the backend for tests marked `@pytest.mark.anyio` (pytest-asyncio 1.x no longer has the `event_loop` fixture — the loop scope is set in `pyproject.toml`) |
| `settings` / `test_database_url` | the project settings and the computed test database address |
| `db_engine` | an async engine on the test database; the schema is created from `Base.metadata` once per run |
| `db_session` | an `AsyncSession` inside an outer transaction: everything is rolled back after the test, tests never see each other's data |
| `app` | a FastAPI instance; `dependency_overrides` are reset after the test |
| `async_client` | an `httpx.AsyncClient` on top of the ASGI application |

`ASGITransport` does not run `lifespan`, so the PostgreSQL and Redis connections are not
established in tests. For routes that need the DB, override the dependency via
`app.dependency_overrides[get_session]` together with the `db_session` fixture.

### Linters, types and git hooks

```bash
uv run ruff check .          # lint
uv run ruff format .         # formatting
uv run mypy apps libs        # types
```

The same checks are wired into git hooks through [pre-commit](https://pre-commit.com).
Set up once from a fresh clone:

```bash
uv tool install pre-commit   # or pipx install pre-commit
pre-commit install --install-hooks
pre-commit install --hook-type commit-msg --hook-type pre-push
```

The fast checks (`ruff`, `mypy`) run on every commit, `pytest` runs once before a push,
and the commit message format is checked at the `commit-msg` stage. Details are in
[CONTRIBUTING.md](CONTRIBUTING.md).

### What CI runs

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) — on every PR into `main` and on
every push to `main`, a single job with the `postgres:16` and `redis:7` services:
`ruff check` → `ruff format --check` → `mypy` → `alembic upgrade head` on a clean
database → creation of `overseer_test` → `pytest --cov`. A failure at any step fails the
job; we do not merge a red CI.

## Status

**MVP in progress.** Everything currently in the repository is a *skeleton*:

- ✅ the FastAPI shell with `GET /health` and a lifespan (opening and closing
  the PostgreSQL and Redis connections);
- ✅ configuration through pydantic-settings, structured logging;
- ✅ async SQLAlchemy 2.x + Alembic on an async engine (`alembic upgrade head`
  works against an empty database);
- ✅ an Arq worker with the `ping` smoke task;
- ✅ docker compose with the whole infrastructure;
- ⏳ ORM models — empty, a stub;
- ⏳ LLM clients — interfaces only, generation is not implemented;
- ⏳ the tool registry and tool calling — not implemented;
- ⏳ `apps/executor` (COM/Playwright) — an empty package with a README;
- ✅ test infrastructure: pytest + pytest-asyncio + pytest-cov, fixtures
  (`db_session`, `async_client`), the `GET /health` smoke test;
- ✅ ruff, mypy, pre-commit and CI on GitHub Actions;
- ⏳ substantive tests — they arrive together with the business logic.

The agent's business logic is added in separate tasks.
