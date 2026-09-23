import numpy as np
import pytest

from streaming_translator.vad import LookbackVAD


@pytest.mark.parametrize('chunk', [160, 640, 8000, 4964])
def test_delayed_onset_keeps_preroll_and_every_sample_once(chunk):
    audio = np.arange(16000 * 8, dtype=np.float32)
    gate = LookbackVAD()
    # Actual pinned VAD onset from the TED recording: 79,392 samples,
    # delivered after 5 seconds. Speech itself starts around 4.14 seconds.
    pending = [(int(5.2*16000), {'start': 79392}), (int(7.2*16000), {'end': 112000})]
    spans = []
    for start in range(0, len(audio), chunk):
        end = min(len(audio), start+chunk)
        events = [event for at, event in pending if start < at <= end]
        spans += gate.feed(audio[start:end], events)
        assert len(gate.buffer) <= gate.hold
    spans += gate.feed(np.empty(0, dtype=np.float32), final=True)
    assert np.array_equal(np.concatenate([part for _, _, _, part in spans]), audio)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    active = [(a, b) for a, b, speech, _ in spans if speech]
    assert active[0][0] == 63392 < 4.14*16000
    assert gate.late_events == 0


def test_short_eof_and_overlapping_speech_padding():
    gate = LookbackVAD()
    audio = np.arange(10000, dtype=np.float32)
    spans = gate.feed(audio, [{'start': 500}, {'end': 2000}, {'start': 4000}], final=True)
    assert len(spans) == 1 and spans[0][2]
    assert np.array_equal(spans[0][3], audio)


def test_silence_stays_bounded_and_accounts_for_eof_tail():
    gate = LookbackVAD()
    count = 0
    for _ in range(2000):
        count += sum(b-a for a,b,_,_ in gate.feed(np.zeros(640, dtype=np.float32)))
        assert len(gate.buffer) <= gate.hold
    count += sum(b-a for a,b,_,_ in gate.feed(np.zeros(73, dtype=np.float32), final=True))
    assert count == 2000*640+73
