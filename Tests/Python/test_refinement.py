import asyncio
import json
import wave

import pytest

from streaming_translator import asr
from streaming_translator import refine as refinement
from streaming_translator.store import SessionStore, file_sha256


def parent_session(path):
    original = SessionStore(path)
    original.initialize(translation_model=None, endpoint='http://127.0.0.1:11434')
    original.ingest(0, [{'text': 'Wrong version.', 'start': 0, 'end': .5}], final=True)
    with wave.open(str(path/'audio.wav'), 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
        audio.writeframes(b'\0\0'*16000)
    original.update(audio_sha256=file_sha256(path/'audio.wav'))
    original.close()


def test_refinement_keeps_parent_and_creates_independent_audio_and_jobs(tmp_path, monkeypatch):
    parent, target = tmp_path/'original', tmp_path/'corrected'
    parent_session(parent)
    before = (parent/'session.sqlite').read_bytes()
    store = SessionStore(target)
    refinement.initialize_refinement(store, parent)
    monkeypatch.setattr(asr, 'validate_resources', lambda _: ({}, {'revision': 'fixture'}))
    result = {'segments': [{'text': 'Correct version.', 'words': [
        {'word': 'Correct', 'start': 0, 'end': .4}, {'word': ' version.', 'start': .4, 'end': .8}]}]}
    monkeypatch.setattr(refinement, 'transcribe_local', lambda *_: (result, 1.5, {'backend': 'test'}))
    asyncio.run(refinement.refine(store, {'model': 'test'}, parent, asyncio.Event()))
    snap = store.snapshot()
    assert snap['asr_complete'] and snap['parent_asr_complete']
    assert snap['parent_session_path'] == str(parent)
    assert snap['segments'][0]['english'] == 'Correct version.'
    assert (parent/'session.sqlite').read_bytes() == before
    assert (target/'audio.wav').read_bytes() == (parent/'audio.wav').read_bytes()
    assert json.loads((target/'refinement.json').read_text()) == result
    store.close()


def test_damaged_recording_is_rejected_before_inference(tmp_path, monkeypatch):
    parent_session(tmp_path/'original')
    store = SessionStore(tmp_path/'corrected')
    refinement.initialize_refinement(store, tmp_path/'original')
    monkeypatch.setattr(asr, 'validate_resources', lambda _: ({}, {'revision': 'fixture'}))
    (tmp_path/'original'/'audio.wav').write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='校验值'):
        asyncio.run(refinement.refine(store, {}, tmp_path/'original', asyncio.Event()))
    assert not store.snapshot()['segments'] and not store.get('asr_complete')
    store.close()


def test_invalid_refinement_does_not_partially_publish():
    with pytest.raises(ValueError, match='时间戳'):
        refinement.result_tokens({'segments': [
            {'text': 'Valid.', 'start': 0, 'end': 1},
            {'text': 'Invalid.', 'start': float('nan'), 'end': 2}]}, 2)


def test_refinement_cannot_replace_its_parent(tmp_path):
    parent_session(tmp_path)
    store = SessionStore(tmp_path)
    with pytest.raises(ValueError, match='新任务'):
        refinement.initialize_refinement(store, tmp_path)
    assert store.snapshot()['segments'][0]['english'] == 'Wrong version.'
    store.close()
