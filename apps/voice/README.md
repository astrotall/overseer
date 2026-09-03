# apps/voice — голосовой клиент (заготовка, кода пока нет)

## Что это

`voice` — **отдельный локальный процесс на машине пользователя, вне Docker**: он слушает
микрофон, распознаёт речь, отправляет её агенту и проговаривает ответ. Отдельным процессом
он нужен по той же причине, что и `executor`, — доступ к железу, которого в контейнере нет:
микрофон и динамики.

Отличие от `executor` — **кроссплатформенность обязательна с первого дня**: разработка идёт
на macOS, целевой деплой — Windows. Поэтому Windows-only зависимости сюда не попадают.

## Он ничего не добавляет на бэкенде

`voice` — это ещё один клиент существующего WebSocket-эндпоинта **`/ws/chat`** (OVE-18),
с тем же конвертом, что и любой другой клиент:

```text
клиент → сервер   {"type": "message", "payload": {"content": "..."}}
сервер → клиент   {"type": "reply",   "payload": {"role": "assistant", "content": "..."}}
сервер → клиент   {"type": "error",   "payload": {"error": "...", "detail": "...", "code": 503}}
```

Схемы конвертов берутся из `libs/schemas/ws.py` — теми же классами, что использует сервер;
локальных копий JSON-формы здесь быть не должно.

## Поток данных

```text
[микрофон] → capture.py → listener.py (wake word + эндпоинтинг) → stt.py
    → ws_client.py ⇄ /ws/chat → tts.py → playback.py → [динамик]
```

## Модель потоков

- callback PortAudio (поток `sounddevice`) — только копирует кадр в ограниченную очередь;
- один рабочий поток — wake word, VAD и нарезка на реплики;
- главный поток — `asyncio`: WebSocket, STT, TTS, воспроизведение.

Через границу поток → цикл едет **готовая реплика**, а не кадры
(`loop.call_soon_threadsafe`). Обоснование — в
[.claude/knowledge/architecture.md](../../.claude/knowledge/architecture.md), раздел
«`apps/voice` — голосовой клиент (OVE-44)».

## Раскладка модулей

| Модуль | Что делает | Тикет |
|---|---|---|
| `main.py` | точка входа, сборка компонентов, `asyncio.run()`, завершение | |
| `config.py` | `VoiceSettings` (`pydantic-settings`, `env_prefix="VOICE_"`) | |
| `state.py` | `idle → listening → thinking → speaking → idle`, гейт полудуплекса | |
| `capture.py` | `sounddevice.InputStream` → ограниченная очередь кадров | OVE-45 |
| `listener.py` | рабочий поток: кадры → wake word → эндпоинтинг → реплика | OVE-45 |
| `wake_word.py` | порт `WakeWordDetector` + реализация | OVE-45 |
| `stt.py` | порт `SpeechToText` | OVE-46 |
| `tts.py` | порт `TextToSpeech` | OVE-47 |
| `playback.py` | `sounddevice.OutputStream` ← очередь воспроизведения | OVE-47 |
| `ws_client.py` | клиент `/ws/chat`: конверты, реконнект | OVE-48 |

## Аудио

Библиотека — **`sounddevice`** (PortAudio через CFFI): колёса с вшитым PortAudio под macOS
и Windows, включая Windows ARM64, ставится без системных зависимостей. Формат внутри
конвейера — **16 kHz, моно, int16**.

Под Linux колеса с PortAudio нет — нужен системный `libportaudio2`, без него
`import sounddevice` падает. Поэтому `sounddevice` импортируется **только** в `capture.py`
и `playback.py`, а `apps/voice/__init__.py` остаётся пустым: остальные модули обязаны
импортироваться и тестироваться в CI на `ubuntu-latest` без звуковой карты.

## Статус

Пустой пакет. Логика приезжает тикетами OVE-45 — OVE-48; движки wake word, STT и TTS ими
же и выбираются — здесь они пока не зафиксированы. В `docker-compose.yml` `voice` не входит
и входить не должен.
