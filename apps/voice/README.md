# apps/voice — голосовой клиент

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
[микрофон] → capture.py → listener.py (wake word) → stt.py (накопление реплики, OVE-46)
    → ws_client.py ⇄ /ws/chat → tts.py → playback.py → [динамик]
```

В OVE-45 конвейер кончается на `listener.py`: он доводит поток до «wake word принят» и
отдаёт `WakeWordEvent`. Накопление кадров после срабатывания и эндпоинтинг (где кончается
реплика) — OVE-46: конец реплики определяет тот же движок, что её распознаёт.

## Модель потоков

- callback PortAudio (поток `sounddevice`) — только копирует кадр в ограниченную очередь,
  пометив его текущим `generation` состояния;
- один рабочий поток (строго один: `start()` на ещё живом потоке падает с `RuntimeError`) —
  wake word; кадр с чужим `generation` до детектора не доходит;
- главный поток — `asyncio`: WebSocket, STT, TTS, воспроизведение.

Через границу поток → цикл едет один объект, а не кадры (`loop.call_soon_threadsafe`): в
OVE-45 это `WakeWordEvent`, с OVE-46 — готовая реплика. Обоснование — в
[.claude/knowledge/architecture.md](../../.claude/knowledge/architecture.md), раздел
«`apps/voice` — голосовой клиент (OVE-44)».

## Раскладка модулей

| Модуль | Что делает | Тикет | Готов |
|---|---|---|---|
| `main.py` | точка входа, сборка компонентов, `asyncio.run()`, завершение | OVE-45 | ✅ пока только просыпается |
| `config.py` | `VoiceSettings` (`pydantic-settings`, `env_prefix="VOICE_"`) | OVE-45 | ✅ |
| `audio.py` | формат конвейера (16 kHz, моно, int16, кадр 80 мс), `QueuedFrame` и `FrameQueue` | OVE-45 | ✅ |
| `state.py` | `idle → listening → thinking → speaking → idle`, гейт полудуплекса, `generation` | OVE-45 | ✅ |
| `capture.py` | `sounddevice.InputStream` → ограниченная очередь кадров | OVE-45 | ✅ |
| `listener.py` | рабочий поток: кадры → wake word → событие срабатывания | OVE-45 | ✅ |
| `wake_word.py` | порт `WakeWordDetector` + реализация на openWakeWord | OVE-45 | ✅ |
| `stt.py` | порт `SpeechToText`, эндпоинтинг реплики | OVE-46 | ⏳ |
| `tts.py` | порт `TextToSpeech` | OVE-47 | ⏳ |
| `playback.py` | `sounddevice.OutputStream` ← очередь воспроизведения | OVE-47 | ⏳ |
| `ws_client.py` | клиент `/ws/chat`: конверты, реконнект | OVE-48 | ⏳ |

## Аудио

Библиотека — **`sounddevice`** (PortAudio через CFFI): колёса с вшитым PortAudio под macOS
и Windows, включая Windows ARM64, ставится без системных зависимостей. Формат внутри
конвейера — **16 kHz, моно, int16**.

Под Linux колеса с PortAudio нет — нужен системный `libportaudio2`, без него
`import sounddevice` падает. Поэтому `sounddevice` импортируется **только** в `capture.py`
и `playback.py`, а `apps/voice/__init__.py` остаётся пустым: остальные модули обязаны
импортироваться и тестироваться в CI на `ubuntu-latest` без звуковой карты.

## Wake word (OVE-45)

Движок — [openWakeWord](https://github.com/dscripka/openWakeWord): код под Apache-2.0,
инференс через `onnxruntime`, вход — 16 kHz моно int16 кадрами по 80 мс. Обоснование
выбора и **лицензионная оговорка** (готовые модели фраз идут под некоммерческой
CC BY-NC-SA 4.0 и до поставки наружу обязаны смениться на свою) —
в [.claude/knowledge/architecture.md](../../.claude/knowledge/architecture.md), раздел
«Движок wake word — openWakeWord (OVE-45)».

Фраза берётся из `VoiceSettings.wake_word_phrase` (`VOICE_WAKE_WORD_PHRASE`), дефолт —
`hey jarvis`. Готовые фразы: `alexa`, `hey mycroft`, `hey jarvis`, `hey rhasspy`, `timer`,
`weather`. Модели на «overseer» не существует — она обучается пайплайном openWakeWord и
подключается через `VOICE_WAKE_WORD_MODEL_PATH` (тогда `VOICE_WAKE_WORD_PHRASE` остаётся
меткой для логов).

### Настройки

| Ключ | Дефолт | Смысл |
|---|---|---|
| `VOICE_WAKE_WORD_PHRASE` | `hey jarvis` | фраза активации |
| `VOICE_WAKE_WORD_MODEL_PATH` | пусто | путь к своей `.onnx`-модели вместо готовой |
| `VOICE_WAKE_WORD_THRESHOLD` | `0.5` | порог: ниже — больше ложных срабатываний, выше — чаще не слышит |
| `VOICE_INPUT_DEVICE` | пусто | индекс или часть имени устройства ввода |
| `VOICE_LOG_LEVEL` | `INFO` | уровень логов голосового клиента |

Порог — единственная ручка чувствительности; калибруется по месту. Ориентир с замеров на
синтезированной речи: сама фраза даёт 0.99+, постороннее предложение со словом «Jarvis»
внутри — 0.2, обычная речь — 0.0.

### Как запустить

```bash
uv sync --group voice                  # openwakeword + sounddevice ставятся отдельно
uv run python -m apps.voice.main
```

Первый запуск скачивает модели (~6 МБ) в `site-packages/openwakeword/resources/models` —
нужен интернет; дальше запускается офлайн. Скажите фразу — в логи уйдёт строка
`voice.wake_word` со скором и `epoch`:

```text
2026-09-03 23:50:22 [info] voice.listening   phrase='hey jarvis' threshold=0.5
2026-09-03 23:50:31 [info] voice.wake_word   phrase='hey jarvis' score=0.87 epoch=0 dropped_frames=0
```

`epoch` пока всегда `0`: он приезжает от `ws_client.py` (OVE-48), а до тех пор
`listener.py` получает заглушку `unset_epoch`. После срабатывания `main.py` держит
состояние `listening` полторы секунды и возвращается в `idle` — в OVE-46 это место займут
запись реплики и STT.

### Разрешения ОС на микрофон

- **macOS**: при первом запуске система спросит доступ к микрофону — разрешение
  выдаётся *терминалу* (или IDE), из которого запущен процесс. Отказали — включается в
  «Системные настройки → Конфиденциальность и безопасность → Микрофон».
- **Windows**: «Параметры → Конфиденциальность → Микрофон», должен быть разрешён доступ
  для классических приложений.

Список устройств — `uv run python -c "import sounddevice; print(sounddevice.query_devices())"`;
нужный индекс или кусок имени кладётся в `VOICE_INPUT_DEVICE`.

## Статус

Работает wake word: микрофон → детектор в рабочем потоке → событие `WakeWordEvent`
(`epoch`, фраза, скор, момент срабатывания) в главный `asyncio`-луп. Дальше — тикетами
OVE-46 (STT и нарезка реплики), OVE-47 (TTS и воспроизведение), OVE-48 (клиент
`/ws/chat`); движки STT и TTS ими же и выбираются, здесь они не зафиксированы. В
`docker-compose.yml` `voice` не входит и входить не должен.
