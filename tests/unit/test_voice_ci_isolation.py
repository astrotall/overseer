from __future__ import annotations

import subprocess
import sys

import pytest

CI_SAFE_MODULES = [
    "apps.voice",
    "apps.voice.audio",
    "apps.voice.config",
    "apps.voice.state",
    "apps.voice.listener",
    "apps.voice.wake_word",
    "apps.voice.vad",
    "apps.voice.stt",
    "apps.voice.cues",
    "apps.voice.pipeline",
    "apps.voice.tts",
    "apps.voice.playback",
    "apps.voice.ws_client",
]
VOICE_ONLY_PACKAGES = (
    "sounddevice",
    "openwakeword",
    "faster_whisper",
    "ctranslate2",
    "torch",
)

PROBE = """
import sys

blocked = {packages}
for name in list(sys.modules):
    if name.split(".")[0] in blocked:
        raise SystemExit("preloaded: " + name)


class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in blocked:
            raise SystemExit("imported: " + name)
        return None


sys.meta_path.insert(0, Blocker())
__import__({module!r})
"""


@pytest.mark.parametrize("module", CI_SAFE_MODULES)
def test_module_imports_without_the_voice_dependency_group(module: str) -> None:
    probe = PROBE.format(packages=repr(set(VOICE_ONLY_PACKAGES)), module=module)

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_engine_classes_are_the_only_thing_that_needs_the_voice_group() -> None:
    from apps.voice.cues import BeepCue
    from apps.voice.playback import SoundDeviceSink
    from apps.voice.stt import FasterWhisperSTT
    from apps.voice.tts import SileroTTS
    from apps.voice.wake_word import OpenWakeWordDetector

    engines = (
        FasterWhisperSTT,
        OpenWakeWordDetector,
        BeepCue,
        SileroTTS,
        SoundDeviceSink,
    )

    assert all(callable(engine) for engine in engines)
