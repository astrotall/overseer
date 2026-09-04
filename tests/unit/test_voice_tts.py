from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from apps.voice import config as voice_config
from apps.voice.config import VoiceSettings
from apps.voice.tts import (
    MAX_CHUNK_CHARS,
    SileroTTS,
    Speech,
    is_speakable,
    split_for_synthesis,
    to_int16,
)


def test_a_short_phrase_stays_one_chunk() -> None:
    assert split_for_synthesis("Отчёт готов.") == ("Отчёт готов.",)


def test_whitespace_is_normalized_before_splitting() -> None:
    assert split_for_synthesis("  Отчёт\n\tготов.  ") == ("Отчёт готов.",)


def test_empty_text_gives_no_chunks() -> None:
    assert split_for_synthesis("   \n ") == ()


@pytest.mark.parametrize("limit", [40, 120, MAX_CHUNK_CHARS])
def test_no_chunk_exceeds_the_limit(limit: int) -> None:
    text = "Отчёт за август сформирован и сохранён в документ Word. " * 40

    chunks = split_for_synthesis(text, limit=limit)

    assert chunks
    assert all(len(chunk) <= limit for chunk in chunks)


def test_chunks_keep_every_word_of_the_original() -> None:
    text = "Первое предложение. Второе предложение! Третье предложение? Четвёртое."

    chunks = split_for_synthesis(text, limit=30)

    assert " ".join(chunks).split() == text.split()


def test_sentences_are_the_first_place_to_break() -> None:
    text = "Первое предложение. Второе предложение."

    assert split_for_synthesis(text, limit=25) == ("Первое предложение.", "Второе предложение.")


def test_a_long_sentence_falls_back_to_clauses() -> None:
    text = "Сначала одно, потом другое, затем третье."

    chunks = split_for_synthesis(text, limit=20)

    assert chunks == ("Сначала одно,", "потом другое,", "затем третье.")


def test_a_clause_longer_than_the_limit_falls_back_to_words() -> None:
    text = "слово " * 10

    chunks = split_for_synthesis(text, limit=11)

    assert chunks == ("слово слово",) * 5


def test_a_single_word_longer_than_the_limit_is_cut_by_length() -> None:
    chunks = split_for_synthesis("абвгдежзий", limit=4)

    assert chunks == ("абвг", "дежз", "ий")


def test_a_non_positive_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        split_for_synthesis("Отчёт готов.", limit=0)


@pytest.mark.parametrize("text", ["Отчёт готов.", "42", "ok"])
def test_text_with_letters_or_digits_is_speakable(text: str) -> None:
    assert is_speakable(text)


@pytest.mark.parametrize("text", ["", "   ", "...", "!!! ?? ,,,", "—"])
def test_text_without_letters_or_digits_is_not_speakable(text: str) -> None:
    assert not is_speakable(text)


def test_a_float_waveform_becomes_int16_at_full_scale() -> None:
    waveform = np.array([0.0, 1.0, -1.0, 0.5], dtype=np.float32)

    samples = to_int16(waveform)

    assert samples.dtype == np.int16
    assert samples.tolist() == [0, 32767, -32767, 16383]


def test_a_waveform_beyond_full_scale_is_clipped_not_wrapped() -> None:
    waveform = np.array([2.5, -3.0], dtype=np.float32)

    samples = to_int16(waveform)

    assert samples.tolist() == [32767, -32767]


def test_speech_reports_its_duration_from_the_sample_count() -> None:
    speech = Speech(samples=np.zeros(48_000, dtype=np.int16), samplerate=24_000)

    assert speech.duration_s == pytest.approx(2.0)


class FakeTorchHub:
    def __init__(self) -> None:
        self.downloads: list[tuple[str, str]] = []

    def download_url_to_file(self, url: str, path: str, progress: bool = True) -> None:
        self.downloads.append((url, path))
        Path(path).write_text(url, encoding="utf-8")


class FakeTorch:
    def __init__(self) -> None:
        self.hub = FakeTorchHub()


def make_downloader(torch: FakeTorch) -> SileroTTS:
    engine = SileroTTS.__new__(SileroTTS)
    engine._torch = torch
    return engine


def test_weights_cached_for_another_url_are_not_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(voice_config, "MODEL_CACHE_DIR", tmp_path)
    first_url = "https://models.silero.ai/models/tts/ru/v1/model.pt"
    second_url = "https://models.silero.ai/models/tts/ru/v2/model.pt"
    torch = FakeTorch()
    downloader = make_downloader(torch)

    first = downloader._ensure_weights(
        first_url, VoiceSettings(tts_model_url=first_url).tts_model_file
    )
    second = downloader._ensure_weights(
        second_url, VoiceSettings(tts_model_url=second_url).tts_model_file
    )

    assert first != second
    assert first.read_text(encoding="utf-8") == first_url
    assert second.read_text(encoding="utf-8") == second_url
    assert torch.hub.downloads == [(first_url, str(first)), (second_url, str(second))]


def test_weights_already_cached_for_the_same_url_are_downloaded_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(voice_config, "MODEL_CACHE_DIR", tmp_path)
    url = "https://models.silero.ai/models/tts/ru/v4_ru.pt"
    torch = FakeTorch()
    downloader = make_downloader(torch)
    weights = VoiceSettings(tts_model_url=url).tts_model_file

    assert downloader._ensure_weights(url, weights) == weights
    assert downloader._ensure_weights(url, weights) == weights
    assert torch.hub.downloads == [(url, str(weights))]
