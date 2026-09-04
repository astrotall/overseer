from __future__ import annotations

import numpy as np
import pytest

from apps.voice.config import VoiceSettings
from apps.voice.stt import (
    AVG_LOGPROB_LIMIT,
    NO_SPEECH_PROB_LIMIT,
    Segment,
    Transcription,
    build_transcription,
    is_meaningful,
    normalize_transcript,
    to_float32,
    transcript_quality,
)


def speech(text: str = "какая погода в Москве") -> Segment:
    return Segment(text=text, no_speech_prob=0.05, avg_logprob=-0.2)


def noise(text: str = " Продолжение следует...") -> Segment:
    return Segment(text=text, no_speech_prob=0.95, avg_logprob=-1.8)


def quiet(text: str, *, no_speech_prob: float = 0.05, avg_logprob: float = -0.2) -> Segment:
    return Segment(text=text, no_speech_prob=no_speech_prob, avg_logprob=avg_logprob)


def from_segments(*segments: Segment, language: str | None = "ru") -> Transcription:
    return build_transcription(segments, language)


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
        segments=(Segment(text=text, no_speech_prob=no_speech_prob, avg_logprob=avg_logprob),),
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


def test_a_quiet_tail_does_not_sink_a_good_long_utterance() -> None:
    utterance = from_segments(
        speech("собери отчёт за август"),
        speech(" и положи его в Word"),
        speech(" а потом отправь почтой"),
        noise(),
    )

    assert is_meaningful(utterance) is True


def test_one_clean_segment_inside_noise_does_not_carry_the_whole_utterance() -> None:
    utterance = from_segments(
        noise(" Субтитры сделал DimaTorzok"),
        noise(" Продолжение следует..."),
        speech(" спасибо за просмотр"),
        noise(" ..."),
        noise(" Продолжение следует..."),
    )

    assert is_meaningful(utterance) is False


def test_a_clean_segment_at_the_very_end_of_noise_does_not_save_it_either() -> None:
    utterance = from_segments(noise(), noise(), speech(" спасибо за просмотр"))

    assert is_meaningful(utterance) is False


def test_a_transcript_of_nothing_but_noise_is_rejected() -> None:
    assert is_meaningful(from_segments(noise(), noise())) is False


def test_half_the_segments_being_speech_is_enough() -> None:
    assert is_meaningful(from_segments(noise(), speech(" включи музыку"))) is True


def test_quality_counts_the_trailing_noise_it_ignored() -> None:
    quality = transcript_quality(from_segments(speech(), noise(), noise()))

    assert quality.segments == 1
    assert quality.speech_segments == 1
    assert quality.trimmed == 2


def test_quality_reports_the_median_of_the_segments_it_kept() -> None:
    quality = transcript_quality(from_segments(noise(), speech(), speech(), noise()))

    assert quality.segments == 3
    assert quality.speech_segments == 2
    assert quality.trimmed == 1
    assert quality.no_speech_prob == pytest.approx(0.05)
    assert quality.avg_logprob == pytest.approx(-0.2)


def test_quality_of_an_utterance_without_segments_is_not_speech() -> None:
    quality = transcript_quality(from_segments())

    assert quality.segments == 0
    assert quality.speech_segments == 0
    assert is_meaningful(Transcription(text="привет", language="ru", segments=())) is False


def test_the_final_text_leaves_out_the_hallucinated_tail() -> None:
    utterance = from_segments(
        speech("собери отчёт за август"),
        speech(" и положи его в Word"),
        noise(" Продолжение следует..."),
        noise(" Субтитры сделал DimaTorzok"),
    )

    assert utterance.text == "собери отчёт за август и положи его в Word"
    assert is_meaningful(utterance) is True


def test_the_dropped_tail_is_still_visible_in_the_metrics() -> None:
    utterance = from_segments(speech("включи музыку"), noise(), noise())

    assert utterance.segments == (speech("включи музыку"), noise(), noise())
    assert transcript_quality(utterance).trimmed == 2


def test_noise_in_the_middle_stays_in_the_text() -> None:
    utterance = from_segments(
        speech("собери отчёт"),
        noise(" Продолжение следует..."),
        speech(" и отправь почтой"),
    )

    assert utterance.text == "собери отчёт Продолжение следует... и отправь почтой"


def test_an_utterance_of_nothing_but_noise_has_no_text_left() -> None:
    utterance = from_segments(noise(), noise())

    assert utterance.text == ""
    assert is_meaningful(utterance) is False


def test_a_quiet_tail_the_model_still_reads_as_speech_stays_in_the_text() -> None:
    utterance = from_segments(
        speech("собери отчёт за август из выгрузки"),
        quiet(" и положи его в ворд", no_speech_prob=0.35, avg_logprob=-0.8),
    )

    assert utterance.text == "собери отчёт за август из выгрузки и положи его в ворд"
    assert is_meaningful(utterance) is True


def test_a_quiet_tail_the_model_doubts_is_lost_from_the_text() -> None:
    utterance = from_segments(
        speech("собери отчёт за август из выгрузки"),
        quiet(" и положи его в ворд", avg_logprob=AVG_LOGPROB_LIMIT - 0.1),
    )

    assert utterance.text == "собери отчёт за август из выгрузки"
    assert is_meaningful(utterance) is True
    assert transcript_quality(utterance).trimmed == 1
