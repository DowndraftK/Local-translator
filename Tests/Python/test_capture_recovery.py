import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import wave

import pytest

from streaming_translator.capture import PCMArchive, PrefixHasher, pcm_prefix_digest, resource_digest, validate_resume
from streaming_translator.store import SessionStore, atomic_json, export_subtitles


def fixture(tmp_path, translation=True):
    store = SessionStore(tmp_path/'session')
    store.initialize(translation_model='fixture' if translation else None, endpoint='http://127.0.0.1:11434')
    config = {'device': 'cpu', 'max_context_tokens': 128}
    for key in ('source_manifest', 'model_manifest'):
        path = tmp_path/(key+'.json')
        path.write_text('{}')
        config[key] = str(path)
    atomic_json(store.directory/'runtime.json', config)
    with wave.open(str(store.directory/'audio.wav'), 'wb') as media:
        media.setnchannels(1); media.setsampwidth(2); media.setframerate(16000)
        media.writeframes(bytes(20*16000*2))
    store.update(resources_digest=resource_digest(config))
    return store, config


def tokens(text, start=0, end=1):
    return [{'text': text, 'start': start, 'end': end}]


def test_capture_archives_without_any_asr_consumer_and_handles_split_samples(tmp_path):
    async def run():
        store, _ = fixture(tmp_path)
        archive = PCMArchive(store)
        reader = asyncio.StreamReader()
        receiver = asyncio.create_task(archive.receive(reader))
        data = bytes(range(256))*1001
        for offset in range(0, len(data), 997):
            reader.feed_data(data[offset:offset+997])
            await asyncio.sleep(0)
        reader.feed_eof()
        await receiver
        with wave.open(str(archive.path), 'rb') as media:
            assert media.getnframes() == len(data)//2
            assert media.readframes(media.getnframes()) == data
        assert archive.samples == store.get('durable_audio_samples') == len(data)//2
        assert archive.done.is_set()
        store.close()
    asyncio.run(run())


@pytest.mark.parametrize('data', [b'', b'abc'])
def test_capture_empty_and_incomplete_eof_are_explicit(tmp_path, data):
    async def run():
        store, _ = fixture(tmp_path)
        archive, reader = PCMArchive(store), asyncio.StreamReader()
        reader.feed_data(data); reader.feed_eof()
        if data:
            with pytest.raises(ValueError, match='不完整'):
                await archive.receive(reader)
        else:
            await archive.receive(reader)
        with wave.open(str(archive.path), 'rb') as media:
            assert media.getnframes() == len(data)//2
        assert archive.samples == len(data)//2 and archive.done.is_set()
        store.close()
    asyncio.run(run())


def test_capture_does_not_acknowledge_failed_disk_sync(tmp_path, monkeypatch):
    async def run():
        store, _ = fixture(tmp_path)
        archive, reader = PCMArchive(store), asyncio.StreamReader()
        reader.feed_data(bytes(100)); reader.feed_eof()
        calls = 0
        sync = os.fsync
        def fail_second(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('disk sync failed')
            sync(fd)
        monkeypatch.setattr(os, 'fsync', fail_second)
        with pytest.raises(OSError, match='disk sync'):
            await archive.receive(reader)
        assert archive.samples == store.get('durable_audio_samples') == 0
        assert archive.error and archive.done.is_set()
        store.close()
    asyncio.run(run())


def test_resume_archives_uncertain_tail_preserves_prefix_and_rejects_stale_lease(tmp_path):
    store, config = fixture(tmp_path)
    store.ingest(0, tokens('Prefix.', 0, 1), final=True, complete=False)
    job = store.claim(); store.finish(job, result={'translation': '前缀'})
    store.ingest(1, tokens(' Pending phrase', 2, 3))
    store.checkpoint(160000, pcm_prefix_digest(store.directory/'audio.wav', 160000), resource_digest(config))
    store.ingest(2, tokens(' ends here.', 11, 12), final=True)
    store.update(asr_complete=False)
    stale = store.claim()
    point = validate_resume(store, config)
    assert store.restart_from_checkpoint(point) == 160000
    assert store.snapshot()['translation_counts']['completed'] == 1
    assert store.snapshot()['pending_english'] == 'Pending phrase'
    assert store.next_sequence() == 2
    history = json.loads(store.db.execute('SELECT payload FROM recovery_history').fetchone()[0])
    assert len(history['segments']) >= 1 and history['translations'][0]['lease'] == stale['lease']
    store.ingest(2, tokens(' repaired.', 11, 12), final=True)
    fresh = store.claim()
    assert not store.finish(stale, result={'translation': '过期'})
    assert store.finish(fresh, result={'translation': '修复'})
    assert store.snapshot()['segments'][0]['chinese'] == '前缀'
    store.close()


@pytest.mark.parametrize('damage', ['audio', 'config', 'manifest', 'short_audio'])
def test_resume_refuses_mismatches_before_touching_saved_transcript(tmp_path, damage):
    store, config = fixture(tmp_path)
    store.ingest(0, tokens('Keep this.'), final=True, complete=False)
    store.checkpoint(160000, pcm_prefix_digest(store.directory/'audio.wav', 160000), resource_digest(config))
    if damage == 'config':
        config['max_context_tokens'] = 256
    elif damage == 'manifest':
        Path(config['model_manifest']).write_text('{"changed":true}')
    elif damage == 'audio':
        with (store.directory/'audio.wav').open('r+b') as stream:
            stream.seek(44); stream.write(b'xx')
    else:
        with (store.directory/'audio.wav').open('r+b') as stream:
            stream.truncate(100)
    before = store.db.total_changes
    with pytest.raises(ValueError):
        validate_resume(store, config)
    assert store.db.total_changes == before
    assert store.snapshot()['segments'][0]['english'] == 'Keep this.'
    store.close()


def test_no_checkpoint_replays_from_zero_and_keeps_history(tmp_path):
    store, config = fixture(tmp_path)
    store.ingest(0, tokens('Uncertain.'))
    assert validate_resume(store, config) is None
    store.restart_from_checkpoint(None)
    assert not store.snapshot()['segments'] and store.next_sequence() == 0
    assert 'Uncertain.' in store.db.execute('SELECT payload FROM recovery_history').fetchone()[0]
    store.close()


def test_incremental_hash_checks_identical_pcm_prefix_and_rejects_backtracking(tmp_path):
    store, _ = fixture(tmp_path)
    path = store.directory/'audio.wav'
    hasher = PrefixHasher(path)
    for sample in (1, 159999, 160000, 320000):
        assert hasher.digest(sample) == pcm_prefix_digest(path, sample)
    with pytest.raises(ValueError):
        hasher.digest(1)
    store.close()


def test_paged_snapshots_keep_global_counts_and_full_exports(tmp_path):
    store, _ = fixture(tmp_path, translation=False)
    for index in range(453):
        store.ingest(index, tokens(f'Number {index}.', index*2, index*2+1), final=True)
    snap = store.publish()
    assert snap['segment_count'] == 453 and snap['segment_offset'] == 253
    assert len(snap['segments']) == 200 and snap['translation_counts']['disabled'] == 453
    atomic_json(store.directory/'view.json', {'offset': 0})
    assert store.publish()['segments'][0]['english'] == 'Number 0.'
    full = export_subtitles(store.snapshot(), 'srt')
    assert 'Number 0.' in full and 'Number 452.' in full and full.count(' --> ') == 453
    store.close()


def test_database_version_and_reopening_are_safe(tmp_path):
    store, _ = fixture(tmp_path)
    path = store.directory/'session.sqlite'
    store.close()
    before = path.read_bytes()
    reopened = SessionStore(path.parent); reopened.close()
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=999')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='版本较新'):
        SessionStore(path.parent)
    assert path.read_bytes() == before


def test_killed_capture_worker_keeps_readable_wave_and_resumable_lock(tmp_path):
    store, config = fixture(tmp_path)
    destination = tmp_path/'live'
    store.close()
    command = [sys.executable, '-m', 'streaming_translator', 'record', '--session', str(destination),
               '--config', str(tmp_path/'session/runtime.json'), '--no-translation']
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]/'runtime'), PYTHONDONTWRITEBYTECODE='1')
    child = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    saved = None
    try:
        data = bytes(range(256))*250
        child.stdin.write(data); child.stdin.flush()
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            if (destination/'snapshot.json').exists():
                snap = json.loads((destination/'snapshot.json').read_text())
                if snap.get('durable_audio_samples') == len(data)//2:
                    break
            time.sleep(.05)
        else:
            raise AssertionError('Capture did not durably archive data')
        child.kill(); child.wait(timeout=5)
        with wave.open(str(destination/'audio.wav'), 'rb') as media:
            assert media.readframes(media.getnframes()) == data
        saved = SessionStore(destination)
        assert validate_resume(saved, config) is None
        result = subprocess.run([sys.executable, '-m', 'streaming_translator', 'status', '--session', str(destination)],
                                env=env, capture_output=True, text=True, timeout=5)
        assert not json.loads(result.stdout)['worker_active']
    finally:
        if child.poll() is None:
            child.kill(); child.wait(timeout=5)
        child.stdin.close()
        if saved:
            saved.close()
