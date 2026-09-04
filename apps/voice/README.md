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
[микрофон] → capture.py → listener.py (wake word + vad.py: нарезка реплики)
    → pipeline.py (stt.py → фильтры) → очередь текста
    → ws_client.py ⇄ /ws/chat (OVE-48) → tts.py → playback.py → [динамик]
```

В OVE-46 конвейер кончается на очереди готового текста: `pipeline.py` кладёт в неё
`Transcript` (текст + `epoch`), а `main.py` пока просто пишет его в лог. Отправку в
`/ws/chat` и реконнект приносит OVE-48 — он подменяет собой потребителя этой очереди, не
трогая всё, что до неё.

## Модель потоков

- callback PortAudio (поток `sounddevice`) — только копирует кадр в ограниченную очередь,
  пометив его текущим `generation` состояния;
- один рабочий поток (строго один: `start()` на ещё живом потоке падает с `RuntimeError`) —
  wake word в состоянии `idle` и накопление реплики в `listening`; кадр с чужим
  `generation` не доходит ни до детектора, ни в реплику;
- главный поток — `asyncio`: STT, фильтры, WebSocket, TTS, воспроизведение.

Через границу поток → цикл едут готовые объекты, а не кадры (`loop.call_soon_threadsafe`):
`WakeWordEvent` и `Utterance`. Обоснование — в
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
| `listener.py` | рабочий поток: кадры → wake word → реплика | OVE-45/46 | ✅ |
| `wake_word.py` | порт `WakeWordDetector` + реализация на openWakeWord | OVE-45 | ✅ |
| `vad.py` | `Endpointer`: энергетический VAD, где кончается реплика | OVE-46 | ✅ |
| `stt.py` | порт `SpeechToText` + реализация на faster-whisper | OVE-46 | ✅ |
| `pipeline.py` | asyncio-оркестрация: реплика → STT → фильтры → очередь текста | OVE-46 | ✅ |
| `cues.py` | короткий сигнал «не расслышал» (заглушка до настоящего TTS) | OVE-46 | ✅ |
| `tts.py` | порт `TextToSpeech` | OVE-47 | ⏳ |
| `playback.py` | `sounddevice.OutputStream` ← очередь воспроизведения | OVE-47 | ⏳ |
| `ws_client.py` | клиент `/ws/chat`: конверты, реконнект | OVE-48 | ⏳ |

## Аудио

Библиотека — **`sounddevice`** (PortAudio через CFFI): колёса с вшитым PortAudio под macOS
и Windows, включая Windows ARM64, ставится без системных зависимостей. Формат внутри
конвейера — **16 kHz, моно, int16**. Тяжёлые движки живут в той же группе зависимостей
`voice` и импортируются лениво, внутри `__init__` своих классов
(`OpenWakeWordDetector`, `FasterWhisperSTT`, `BeepCue`), — сами модули `wake_word.py`,
`stt.py` и `cues.py` в CI импортируются без них.

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

## Реплика и распознавание (OVE-46)

После wake word рабочий поток копит кадры, пока `Endpointer` (`vad.py`) не решит, что
человек замолчал: RMS кадра против одного порога, конец реплики — по паузе, а не по
фиксированной длительности. Готовая реплика уезжает в `asyncio`, там её распознаёт
**faster-whisper** (`stt.py`), а `pipeline.py` решает, годится ли результат для отправки.

VAD намеренно простой: искать речь в записи не его работа — это уже делает `vad_filter`
внутри faster-whisper, — ему нужно только поймать конец реплики. Обоснование выбора (и
почему нет адаптивной оценки шума) — в
[.claude/knowledge/architecture.md](../../.claude/knowledge/architecture.md), раздел
«Захват реплики, эндпоинтинг и STT (OVE-46)».

Пустой текст, одни знаки препинания или неуверенность самой модели — реплика **не
отправляется**: звучит короткий двойной сигнал «не расслышал» (`cues.py`), клиент
возвращается в `idle`. Это заглушка до настоящего TTS из OVE-47: молчание в ответ на
«hey jarvis» неотличимо от сломанного клиента.

### Настройки STT и VAD

| Ключ | Дефолт | Смысл |
|---|---|---|
| `VOICE_STT_MODEL` | `small` | модель faster-whisper: `tiny`/`base`/`small`/`medium`/`large-v3` или путь |
| `VOICE_STT_DEVICE` | `auto` | устройство инференса CTranslate2: `auto`, `cpu`, `cuda` |
| `VOICE_STT_COMPUTE_TYPE` | `int8` | квантизация: `int8`, `int8_float16`, `float16`, `float32` |
| `VOICE_STT_LANGUAGE` | `ru` | язык распознавания; пусто — автоопределение |
| `VOICE_VAD_SPEECH_RMS` | `300.0` | порог речи по RMS кадра (шкала int16) |
| `VOICE_VAD_SILENCE_S` | `0.8` | пауза, после которой реплика считается законченной |
| `VOICE_VAD_START_TIMEOUT_S` | `3.0` | сколько ждать начала речи после wake word |
| `VOICE_VAD_MAX_UTTERANCE_S` | `30.0` | потолок длины реплики |

`VOICE_VAD_SPEECH_RMS` — единственная ручка калибровки VAD, аналог порога wake word.
Срезает конец фразы или ловит шум вентилятора — крутить её. Ориентир: синтезированная
речь на 16 kHz даёт RMS ~4500, комнатный шум — десятки.

`VOICE_STT_MODEL` — компромисс, а не оптимум. На macOS/CPU с `int8` модель `small`
распознаёт реплику в 1.4 с за ~2.4 с, в 15 с — за ~3 с; `medium` заметно точнее на
русском, но добавляет задержку, которую в диалоге слышно.

Первый запуск скачивает веса модели (`small` — около 460 МБ) в кэш Hugging Face.

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
`listener.py` и `pipeline.py` получают заглушку `unset_epoch`. После срабатывания скажите
запрос — распознанный текст уйдёт в лог строкой `voice.transcript`:

```text
2026-09-04 17:20:44 [info] voice.wake_word_detected  phrase='hey jarvis' score=0.98 epoch=0
2026-09-04 17:20:47 [info] voice.utterance_captured  duration_s=2.88 outcome=speech truncated=False
2026-09-04 17:20:48 [info] voice.transcript          text='Какая погода в Москве?' language=ru
```

Дальше этой строки текст пока не идёт: отправку в `/ws/chat` приносит OVE-48.

### Разрешения ОС на микрофон

- **macOS**: при первом запуске система спросит доступ к микрофону — разрешение
  выдаётся *терминалу* (или IDE), из которого запущен процесс. Отказали — включается в
  «Системные настройки → Конфиденциальность и безопасность → Микрофон».
- **Windows**: «Параметры → Конфиденциальность → Микрофон», должен быть разрешён доступ
  для классических приложений.

Список устройств — `uv run python -c "import sounddevice; print(sounddevice.query_devices())"`;
нужный индекс или кусок имени кладётся в `VOICE_INPUT_DEVICE`.

## Статус

Работает путь от микрофона до текста: детектор wake word в рабочем потоке → нарезка реплики
по паузе → faster-whisper → фильтр «ничего не понял» → очередь готового текста. Дальше —
тикетами OVE-47 (TTS и воспроизведение) и OVE-48 (клиент `/ws/chat`, реконнект и живой
`epoch`); движок TTS выбирает свой тикет, здесь он не зафиксирован. В `docker-compose.yml`
`voice` не входит и входить не должен.
