from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.voice.config import VoiceSettings
from apps.voice.wake_word import phrase_to_model_name


def test_wake_word_phrase_has_a_default_backed_by_a_ready_made_model() -> None:
    assert VoiceSettings().wake_word_phrase == "hey jarvis"


def test_wake_word_phrase_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOICE_WAKE_WORD_PHRASE", "hey mycroft")

    assert VoiceSettings().wake_word_phrase == "hey mycroft"


def test_wake_word_threshold_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOICE_WAKE_WORD_THRESHOLD", "0.7")

    assert VoiceSettings().wake_word_threshold == pytest.approx(0.7)


def test_wake_word_threshold_must_be_a_probability() -> None:
    with pytest.raises(ValidationError, match="wake_word_threshold"):
        VoiceSettings(wake_word_threshold=1.4)


def test_custom_model_path_is_read_as_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_WAKE_WORD_MODEL_PATH", "models/overseer.onnx")

    assert VoiceSettings().wake_word_model_path == Path("models/overseer.onnx")


def test_voice_settings_ignore_the_settings_of_the_server_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    assert VoiceSettings().log_level == "INFO"


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("hey jarvis", "hey_jarvis"),
        ("  Hey   Jarvis ", "hey_jarvis"),
        ("alexa", "alexa"),
    ],
)
def test_phrase_maps_to_the_openwakeword_model_name(phrase: str, expected: str) -> None:
    assert phrase_to_model_name(phrase) == expected


def test_empty_optional_keys_from_env_file_mean_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOICE_WAKE_WORD_MODEL_PATH", "")
    monkeypatch.setenv("VOICE_INPUT_DEVICE", "  ")

    settings = VoiceSettings()

    assert settings.wake_word_model_path is None
    assert settings.input_device is None


@pytest.mark.parametrize("max_utterance_s", [3.0, 1.5])
def test_the_utterance_ceiling_must_leave_room_for_the_start_timeout(
    max_utterance_s: float,
) -> None:
    with pytest.raises(ValidationError, match="vad_max_utterance_s"):
        VoiceSettings(vad_start_timeout_s=3.0, vad_max_utterance_s=max_utterance_s)


def test_an_impossible_vad_pair_is_rejected_before_the_endpointer_sees_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOICE_VAD_START_TIMEOUT_S", "10")
    monkeypatch.setenv("VOICE_VAD_MAX_UTTERANCE_S", "10")

    with pytest.raises(ValidationError, match="vad_max_utterance_s"):
        VoiceSettings()


def test_a_workable_vad_pair_passes() -> None:
    settings = VoiceSettings(vad_start_timeout_s=3.0, vad_max_utterance_s=3.5)

    assert settings.vad_max_utterance_s == pytest.approx(3.5)
