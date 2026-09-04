from __future__ import annotations

import numpy as np
import pytest

from apps.voice.config import VoiceSettings
from apps.voice.stt import (
    AVG_LOGPROB_LIMIT,
    NO_SPEECH_PROB_LIMIT,
    Transcription,
    is_meaningful,
    normalize_transcript,
    to_float32,
)


def transcription(
    text: str,
    *,
    no_speech_prob: float = 0.05,
    avg_logprob: float = -0.2,
    language: str | None = "ru",
) -> Transcription:
    return Transcription(
        text=text,
        language=language,
        no_speech_prob=no_speech_prob,
        avg_logprob=avg_logprob,
    )


def test_to_float32_normalises_int16_into_the_unit_interval() -> None:
    samples = np.array([-32768, 0, 32767], dtype=np.int16)

    converted = to_float32(samples)

    assert converted.dtype == np.float32
    assert converted[0] == pytest.approx(-1.0)
    assert converted[1] == pytest.approx(0.0)
    assert converted[2] == pytest.approx(1.0, abs=1e-4)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  какая погода в Москве  ", "какая погода в Москве"),
        ("два\nслова", "два слова"),
        (" собери\t\tотчёт ", "собери отчёт"),
    ],
)
def test_normalize_transcript_squeezes_whitespace(raw: str, expected: str) -> None:
    assert normalize_transcript(raw) == expected


def test_a_real_phrase_is_meaningful() -> None:
    assert is_meaningful(transcription("какая погода в Москве")) is True


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_an_empty_transcript_is_not_meaningful(text: str) -> None:
    assert is_meaningful(transcription(text)) is False


@pytest.mark.parametrize("text", [".", "...", "!?", "— ...", "  -  "])
def test_a_transcript_without_letters_is_not_meaningful(text: str) -> None:
    assert is_meaningful(transcription(text)) is False


def test_a_single_character_is_not_meaningful() -> None:
    assert is_meaningful(transcription("а")) is False


def test_a_confident_hallucination_is_rejected_by_the_no_speech_probability() -> None:
    hallucination = transcription(
        "Продолжение следует...",
        no_speech_prob=NO_SPEECH_PROB_LIMIT + 0.1,
    )

    assert is_meaningful(hallucination) is False


def test_a_transcript_the_model_is_unsure_about_is_rejected() -> None:
    assert is_meaningful(transcription("бу бу бу", avg_logprob=AVG_LOGPROB_LIMIT - 0.5)) is False


def test_the_gates_sit_exactly_on_their_limits() -> None:
    assert is_meaningful(transcription("привет", no_speech_prob=NO_SPEECH_PROB_LIMIT)) is True
    assert is_meaningful(transcription("привет", avg_logprob=AVG_LOGPROB_LIMIT)) is True


def test_digits_alone_still_count_as_speech() -> None:
    assert is_meaningful(transcription("2026")) is True


def test_stt_settings_default_to_russian_and_a_cpu_friendly_model() -> None:
    settings = VoiceSettings()

    assert settings.stt_language == "ru"
    assert settings.stt_model == "small"
    assert settings.stt_compute_type == "int8"


@pytest.mark.parametrize("raw", ["", "   "])
def test_a_blank_language_means_autodetect(raw: str) -> None:
    assert VoiceSettings(stt_language=raw).stt_language is None


def test_the_language_is_normalised_to_lower_case() -> None:
    assert VoiceSettings(stt_language=" RU ").stt_language == "ru"
