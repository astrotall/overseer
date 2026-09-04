from __future__ import annotations

import numpy as np
import pytest

from apps.voice.audio import SAMPLE_RATE, Int16Frame
from apps.voice.config import VoiceSettings
from apps.voice.vad import Endpoint, Endpointer, EndpointOutcome, frame_rms

FRAME_S = 0.08
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_S)
SPEECH_AMPLITUDE = 5000
ROOM_AMPLITUDE = 20


def tone(amplitude: int, samples: int = FRAME_SAMPLES) -> Int16Frame:
    time = np.arange(samples, dtype=np.float64) / SAMPLE_RATE
    return (np.sin(2.0 * np.pi * 220.0 * time) * amplitude).astype(np.int16)


def speech() -> Int16Frame:
    return tone(SPEECH_AMPLITUDE)


def room() -> Int16Frame:
    return tone(ROOM_AMPLITUDE)


def make_endpointer(
    *,
    speech_rms: float = 300.0,
    silence_s: float = 0.8,
    start_timeout_s: float = 3.0,
    max_duration_s: float = 30.0,
) -> Endpointer:
    return Endpointer(
        speech_rms=speech_rms,
        silence_s=silence_s,
        start_timeout_s=start_timeout_s,
        max_duration_s=max_duration_s,
    )


def feed_seconds(endpointer: Endpointer, seconds: float, frame: Int16Frame) -> Endpoint | None:
    for _ in range(round(seconds / FRAME_S)):
        endpoint = endpointer.feed(frame)
        if endpoint is not None:
            return endpoint

    return None


def test_frame_rms_of_an_empty_frame_is_zero() -> None:
    assert frame_rms(np.empty(0, dtype=np.int16)) == 0.0


def test_endpointer_hears_speech_and_not_the_room() -> None:
    endpointer = make_endpointer()

    assert endpointer.is_speech(speech()) is True
    assert endpointer.is_speech(room()) is False


def test_endpointer_stays_deaf_to_a_room_louder_than_the_threshold() -> None:
    quiet = make_endpointer(speech_rms=300.0)
    calibrated = make_endpointer(speech_rms=3000.0)
    noisy_room = tone(1000)

    assert quiet.is_speech(noisy_room) is True
    assert calibrated.is_speech(noisy_room) is False


@pytest.mark.parametrize("speech_s", [2.0, 15.0])
def test_endpointer_keeps_the_whole_utterance_short_and_long(speech_s: float) -> None:
    endpointer = make_endpointer(silence_s=0.8)
    lead_in_s = 0.4

    assert feed_seconds(endpointer, lead_in_s, room()) is None
    assert feed_seconds(endpointer, speech_s, speech()) is None
    endpoint = feed_seconds(endpointer, 2.0, room())

    assert endpoint is not None
    assert endpoint.outcome is EndpointOutcome.SPEECH
    assert endpoint.truncated is False
    assert endpoint.duration_s >= lead_in_s + speech_s
    assert endpoint.duration_s <= lead_in_s + speech_s + 0.8 + 3 * FRAME_S
    assert endpoint.samples.size == pytest.approx(
        SAMPLE_RATE * endpoint.duration_s, abs=FRAME_SAMPLES
    )


def test_endpointer_survives_a_pause_shorter_than_the_silence_window() -> None:
    endpointer = make_endpointer(silence_s=0.8)

    assert feed_seconds(endpointer, 3.0, speech()) is None
    assert feed_seconds(endpointer, 0.6, room()) is None
    assert feed_seconds(endpointer, 3.0, speech()) is None
    endpoint = feed_seconds(endpointer, 2.0, room())

    assert endpoint is not None
    assert endpoint.duration_s >= 6.6
    assert endpoint.duration_s <= 6.6 + 0.8 + 3 * FRAME_S


def test_endpointer_reports_no_speech_when_the_wake_word_was_a_false_alarm() -> None:
    endpointer = make_endpointer(start_timeout_s=3.0)

    endpoint = feed_seconds(endpointer, 5.0, room())

    assert endpoint is not None
    assert endpoint.outcome is EndpointOutcome.NO_SPEECH
    assert endpoint.samples.size == 0
    assert endpoint.duration_s == pytest.approx(3.0, abs=FRAME_S)


def test_endpointer_waits_out_the_whole_start_timeout_before_giving_up() -> None:
    endpointer = make_endpointer(start_timeout_s=3.0, silence_s=0.8)

    assert feed_seconds(endpointer, 2.5, room()) is None
    assert feed_seconds(endpointer, 2.0, speech()) is None
    endpoint = feed_seconds(endpointer, 2.0, room())

    assert endpoint is not None
    assert endpoint.outcome is EndpointOutcome.SPEECH


def test_endpointer_truncates_an_utterance_that_runs_past_the_ceiling() -> None:
    endpointer = make_endpointer(max_duration_s=5.0)

    endpoint = feed_seconds(endpointer, 10.0, speech())

    assert endpoint is not None
    assert endpoint.outcome is EndpointOutcome.SPEECH
    assert endpoint.truncated is True
    assert endpoint.duration_s == pytest.approx(5.0, abs=FRAME_S)


def test_endpointer_starts_clean_after_it_has_reported_an_endpoint() -> None:
    endpointer = make_endpointer()

    feed_seconds(endpointer, 1.0, speech())
    first = feed_seconds(endpointer, 2.0, room())
    assert first is not None
    assert endpointer.speech_started is False

    feed_seconds(endpointer, 1.0, speech())
    second = feed_seconds(endpointer, 2.0, room())

    assert second is not None
    assert second.duration_s == pytest.approx(first.duration_s, abs=FRAME_S)
    assert second.samples.size == first.samples.size


def test_endpointer_ignores_an_empty_frame() -> None:
    endpointer = make_endpointer()

    assert endpointer.feed(np.empty(0, dtype=np.int16)) is None
    assert endpointer.speech_started is False


def test_endpointer_from_settings_reads_the_voice_config() -> None:
    settings = VoiceSettings(
        vad_speech_rms=111.0,
        vad_silence_s=0.5,
        vad_start_timeout_s=1.0,
        vad_max_utterance_s=4.0,
    )
    endpointer = Endpointer.from_settings(settings)

    endpoint = feed_seconds(endpointer, 3.0, room())

    assert endpoint is not None
    assert endpoint.outcome is EndpointOutcome.NO_SPEECH
    assert endpoint.duration_s == pytest.approx(1.0, abs=FRAME_S)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"speech_rms": 0.0}, "speech_rms"),
        ({"silence_s": 0.0}, "silence_s"),
        ({"start_timeout_s": 0.0}, "start_timeout_s"),
        ({"max_duration_s": 1.0, "start_timeout_s": 3.0}, "max_duration_s"),
    ],
)
def test_endpointer_rejects_nonsense_settings(kwargs: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        make_endpointer(**kwargs)
