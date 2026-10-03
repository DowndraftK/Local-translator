"""Ownership and ordering regressions; no model downloads or GPU inference."""
import asyncio
import importlib
import sys
from types import SimpleNamespace
import json

import pytest

from streaming_translator.lifecycle import release_speech_resources
from streaming_translator.lifecycle import install_encoder_lifetime_adapter


def test_release_drops_worker_model_holders_after_drain(monkeypatch):
    calls = []
    holder = SimpleNamespace(model=object(), model_path='local')
    core = SimpleNamespace(TranscriptionEngine=SimpleNamespace(reset=lambda: calls.append('reset')))
    monkeypatch.setitem(sys.modules, 'whisperlivekit.core', core)
    monkeypatch.setitem(sys.modules, 'mlx_whisper.transcribe', SimpleNamespace(ModelHolder=holder))
    monkeypatch.setitem(sys.modules, 'torch', None)
    monkeypatch.setitem(sys.modules, 'mlx.core', None)
    evidence = release_speech_resources()
    assert calls == ['reset']
    assert holder.model is None and holder.model_path is None
    assert evidence['release_seconds'] >= 0


def test_release_does_not_load_speech_dependencies_for_reader(monkeypatch):
    for name in ['whisperlivekit.core', 'mlx_whisper.transcribe', 'torch', 'mlx.core']:
        monkeypatch.delitem(sys.modules, name, raising=False)
    release_speech_resources()
    assert not any(name in sys.modules for name in ['whisperlivekit.core', 'mlx_whisper.transcribe', 'torch', 'mlx.core'])


def test_encoder_adapter_keeps_original_loader_arguments_and_encoder():
    encoder = object()
    model = SimpleNamespace(encoder=encoder, decoder=object(), dims=object())
    calls = []
    def load(*args, **kwargs):
        calls.append((args, kwargs))
        return model
    backend = SimpleNamespace(load_mlx_encoder=load)
    mx = SimpleNamespace(get_active_memory=lambda: 100 if model.decoder else 40,
                         synchronize=lambda: None, clear_cache=lambda: None)
    evidence = install_encoder_lifetime_adapter(backend, mx)
    assert install_encoder_lifetime_adapter(backend, mx) is evidence
    loaded = backend.load_mlx_encoder('fixed-model', dtype='original-dtype')
    assert loaded is model and loaded.encoder is encoder
    assert loaded.decoder is None
    assert calls == [(('fixed-model',), {'dtype': 'original-dtype'})]
    assert evidence['mlx_active_before_bytes'] == 100
    assert evidence['mlx_active_after_bytes'] == 40


@pytest.mark.parametrize('fail', [False, True])
def test_release_runs_after_drain_before_translation_gate_opens(tmp_path, monkeypatch, fail):
    worker = importlib.import_module('streaming_translator.__main__')
    asr = importlib.import_module('streaming_translator.asr')
    lifecycle = importlib.import_module('streaming_translator.lifecycle')
    from streaming_translator.store import SessionStore
    calls = []
    async def recognize(store, *a, **kw):
        calls.append('drain')
        store.ingest(0, [{'text': 'Saved tail.', 'start': 0, 'end': 1}], final=True)
        store.update(asr_complete=not fail)
        if fail:
            raise ValueError('controlled inference failure after safe prefix')
    def release():
        assert calls == ['drain']
        calls.append('release')
        return {'released': True}
    async def translate(store, done, stop):
        await done.wait()
        assert calls == ['drain', 'release']
        calls.append('translate')
    monkeypatch.setattr(asr, 'recognize', recognize)
    monkeypatch.setattr(lifecycle, 'release_speech_resources', release)
    monkeypatch.setattr(worker, 'translation_loop', translate)
    config = tmp_path/'config.json'; config.write_text('{}')
    args = SimpleNamespace(command='run', max_audio_seconds=None, config=config,
        no_vad=False, device=None, dtype=None, coalesce_seconds=None, max_context_tokens=None,
        no_translation=True, translation_model='unused', endpoint='http://127.0.0.1:11434',
        stdin_pcm=False, paced=False, input=None)
    store = SessionStore(tmp_path/'session')
    try:
        assert asyncio.run(worker.execute(args, store)) == int(fail)
        assert calls == ['drain', 'release', 'translate']
        assert store.snapshot()['segments'][0]['english'] == 'Saved tail.'
        assert store.get('speech_release') == {'released': True}
        assert store.get('state') == ('failed' if fail else 'completed')
    finally:
        store.close()
