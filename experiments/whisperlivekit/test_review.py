"""Model-free behavioral checks against the pinned, unmodified WLK snapshot.

These exercise upstream code, with translation generation replaced by a recorder.
They measure neither recognition quality nor translation quality/latency.
See README.md for preparation and the three deliberately exposed limitations.
"""

import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

SHA = "363e4f6d029694d9c81ae548beddd9d3c88a3637"
ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get(
    "WLK_SOURCE_DIR",
    ROOT / "artifacts" / "whisperlivekit-review-20260915" / f"WhisperLiveKit-{SHA}",
)).resolve()
if not (SOURCE / "whisperlivekit" / "processing_queue.py").is_file():
    raise RuntimeError(f"Prepare the pinned upstream snapshot first: {SOURCE}")
sys.path.insert(0, str(SOURCE))

from whisperlivekit.diff_protocol import DiffTracker
from whisperlivekit.local_agreement.online_asr import HypothesisBuffer, OnlineASRProcessor
from whisperlivekit.processing_queue import PipelineClosed, PipelineOverloaded, ProcessingQueue, SENTINEL
from whisperlivekit.timed_objects import ASRToken, FrontData, Segment, Silence, State
from whisperlivekit.translation_mlx_llm_mt import MlxLlmTranslation
from whisperlivekit.translation_processor import run_translation


def tokens(text, start=0.0, step=0.25):
    return [ASRToken(start + i * step, start + (i + 1) * step, word)
            for i, word in enumerate(text.split())]


def commit(buffer, hypothesis):
    buffer.insert(hypothesis, 0)
    return buffer.flush()


def translator(monkeypatch):
    engine = MlxLlmTranslation(source_language="en", target_language="zh", warmup=False)
    calls = []

    def generate(text):
        calls.append(text)
        return "FAKE_MT: " + text

    monkeypatch.setattr(engine, "_translate_text", generate)
    return engine, calls


def test_changed_hypothesis_does_not_commit_its_unstable_tail():
    buffer = HypothesisBuffer()
    assert commit(buffer, tokens("We can refund")) == []
    stable = commit(buffer, tokens("We cannot refund"))
    assert [t.text for t in stable] == ["We"]
    stable = commit(buffer, tokens("We cannot refund"))
    assert [t.text for t in stable] == ["cannot", "refund"]


def test_boundary_overlap_is_removed():
    buffer = HypothesisBuffer()
    first = [ASRToken(0, 1, "Hello")]
    commit(buffer, first)
    commit(buffer, first)
    # Same occurrence reappears at the boundary due to timestamp movement.
    overlap = [ASRToken(0.95, 1.05, "Hello"), ASRToken(1.1, 1.4, "world")]
    commit(buffer, overlap)
    assert [t.text for t in commit(buffer, overlap)] == ["world"]


@pytest.mark.xfail(strict=True, reason="Pinned LocalAgreement removes a distinct repeated word within its 1 s text-dedup gate")
def test_intentional_rapid_repetition_must_survive():
    buffer = HypothesisBuffer()
    first = [ASRToken(0, 0.2, "very")]
    commit(buffer, first)
    commit(buffer, first)
    later = first + [ASRToken(0.35, 0.55, "very"), ASRToken(0.6, 0.9, "important")]
    commit(buffer, later)
    assert [t.text for t in commit(buffer, later)] == ["very", "important"]


def test_asr_finish_emits_pending_tokens_once_without_new_inference():
    asr = SimpleNamespace(tokenizer=None, confidence_validation=False,
                          buffer_trimming="segment", buffer_trimming_sec=15, sep=" ")
    processor = OnlineASRProcessor(asr)
    processor.insert_audio_chunk(np.zeros(16000, dtype=np.float32))
    processor.transcript_buffer.buffer = tokens("last words")
    tail, end = processor.finish()
    assert [t.text for t in tail] == ["last", "words"]
    assert end == 1.0
    assert processor.finish()[0] == []


def test_queue_pressure_waits_and_preserves_audio_order():
    async def scenario():
        queue = ProcessingQueue("review", max_samples=4, timeout=1)
        a = np.array([1, 2, 3, 4], dtype=np.float32)
        b = np.array([5, 6], dtype=np.float32)
        await queue.put(a)
        producer = asyncio.create_task(queue.put(b))
        await asyncio.sleep(0)
        assert not producer.done()
        assert np.array_equal(await queue.get(), a)
        queue.task_done()
        await asyncio.wait_for(producer, 1)
        assert np.array_equal(await queue.get(), b)
        queue.task_done()
        assert queue.peak_samples == 4 and queue.queued_samples == 0
        await queue.join()

    asyncio.run(scenario())


def test_queue_overload_is_an_explicit_error():
    async def scenario():
        errors = []
        queue = ProcessingQueue("review", max_samples=2, timeout=0.01, on_overload=errors.append)
        await queue.put(np.zeros(2))
        with pytest.raises(PipelineOverloaded, match="backlog"):
            await queue.put(np.ones(1))
        assert len(errors) == 1 and queue.queued_samples == 2
        queue.close()

    asyncio.run(scenario())


def test_queue_close_discards_pending_audio_and_wakes_producer():
    async def scenario():
        queue = ProcessingQueue("review", max_samples=2, timeout=1)
        await queue.put(np.zeros(2))
        producer = asyncio.create_task(queue.put(np.ones(1)))
        await asyncio.sleep(0)
        assert not producer.done()
        queue.close()
        with pytest.raises(PipelineClosed):
            await asyncio.wait_for(producer, 1)
        assert queue.empty() and queue.queued_samples == 0
        await queue.join()

    asyncio.run(scenario())


def test_refund_clause_waits_for_the_completed_sentence(monkeypatch):
    engine, calls = translator(monkeypatch)
    head = "A refund is available if the cancellation request is received"
    tail = "at least 14 days before the course begins."
    engine.insert_tokens(tokens(head))
    assert engine.process()[0] is None and calls == []
    engine.insert_tokens(tokens(tail, start=3))
    result, _ = engine.process()
    assert calls == [head + " " + tail]
    assert result.text == "FAKE_MT: " + head + " " + tail


@pytest.mark.parametrize("head,tail", [
    ("Dr.", "Smith will explain."),
    ("The value is 3.14", "metres."),
])
@pytest.mark.xfail(strict=True, reason="Pinned has_punctuation treats any period inside an ASR token as a sentence boundary")
def test_abbreviation_and_decimal_must_not_end_a_sentence(monkeypatch, head, tail):
    engine, calls = translator(monkeypatch)
    engine.insert_tokens(tokens(head))
    assert engine.process()[0] is None and calls == []
    engine.insert_tokens(tokens(tail, start=2))
    engine.process()
    assert calls == [head + " " + tail]


def test_failed_translation_retries_without_repeating_success(monkeypatch):
    engine, calls = translator(monkeypatch)

    def generate(text):
        calls.append(text)
        if text == "Two." and calls.count(text) == 1:
            raise RuntimeError("simulated failure")
        return "FAKE_MT: " + text

    monkeypatch.setattr(engine, "_translate_text", generate)
    engine.insert_tokens(tokens("One. Two. Tail"))
    result, _ = engine.process()
    assert result.text == "FAKE_MT: One."
    assert "simulated failure" in engine.error
    result, _ = engine.finish()
    assert result.text == "FAKE_MT: Two. FAKE_MT: Tail"
    assert calls == ["One.", "Two.", "Two.", "Tail"]
    assert not engine.error and engine.finish()[0] is None


def test_translation_worker_flushes_pause_and_unpunctuated_eof(monkeypatch):
    engine, calls = translator(monkeypatch)

    async def scenario():
        queue = ProcessingQueue("translation")
        state = State()
        events = tokens("First") + [Silence(start=1, end=2, is_starting=True, has_ended=True)]
        events += tokens("Last words", start=2) + [SENTINEL]
        for event in events:
            await queue.put(event)
        await asyncio.wait_for(run_translation(queue, engine, state, asyncio.Lock()), 2)
        assert calls == ["First", "Last words"]
        assert [t.text for t in state.new_translation] == ["FAKE_MT: First", "FAKE_MT: Last words"]
        assert engine.finish()[0] is None

    asyncio.run(scenario())


def test_translation_worker_can_end_with_an_untranslated_tail(monkeypatch):
    engine, calls = translator(monkeypatch)

    def fail(text):
        calls.append(text)
        raise RuntimeError("persistent failure")

    monkeypatch.setattr(engine, "_translate_text", fail)

    async def scenario():
        queue = ProcessingQueue("translation")
        state = State()
        for event in tokens("Last words") + [SENTINEL]:
            await queue.put(event)
        await asyncio.wait_for(run_translation(queue, engine, state, asyncio.Lock()), 2)
        assert state.new_translation == []
        assert calls == ["Last words"] and "persistent failure" in engine.error
        # Preserved only in memory; worker completion does not retry forever.
        assert engine._pending_finals[0][0] == "Last words"

    asyncio.run(scenario())


def apply_diff(lines, message):
    """Test client model: new_lines is a replacement suffix, not append-only."""
    if message["type"] == "snapshot":
        return message["lines"]
    retained = lines[message.get("lines_pruned", 0):]
    suffix = message.get("new_lines", [])
    prefix_count = message["n_lines"] - len(suffix)
    assert 0 <= prefix_count <= len(retained)
    return retained[:prefix_count] + suffix


def front(*texts):
    return FrontData(lines=[Segment(start=i, end=i+1, text=t, speaker=-1)
                            for i, t in enumerate(texts)])


def test_diff_revised_last_line_is_replaced_without_duplicate():
    tracker = DiffTracker()
    lines = apply_diff([], tracker.to_message(front("First.", "A draft")))
    update = tracker.to_message(front("First.", "A corrected sentence."))
    lines = apply_diff(lines, update)
    assert [line["text"] for line in lines] == ["First.", "A corrected sentence."]
    assert update["seq"] == 2
    # The protocol's docstring append-only recipe would leave 3 lines here.
    assert update["n_lines"] == 2 and len(update["new_lines"]) == 1


def test_diff_pruning_keeps_a_separate_application_archive():
    tracker = DiffTracker()
    initial = front("First.", "Second.")
    lines = apply_diff([], tracker.to_message(initial))
    archive = list(lines)
    pruned = FrontData(lines=initial.lines[1:])
    update = tracker.to_message(pruned)
    lines = apply_diff(lines, update)
    assert [line["text"] for line in lines] == ["Second."]
    assert [line["text"] for line in archive] == ["First.", "Second."]
    assert update["lines_pruned"] == 1
