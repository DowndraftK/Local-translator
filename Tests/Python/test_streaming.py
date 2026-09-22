import asyncio
import json
from pathlib import Path
import sqlite3

import httpx
import pytest

from streaming_translator.segmentation import take_segments
from streaming_translator.store import SessionStore, export_subtitles
from streaming_translator.ollama import LocalTranslator, validate_endpoint


def token(text, start=0, end=1):
    return {'text': text, 'start': start, 'end': end}


@pytest.mark.parametrize('pieces,expected', [
    ([' Dr.', ' Smith', ' measured', ' 3.', '14', ' milligrams.', ' Next', ' sentence.'],
     ['Dr. Smith measured 3.14 milligrams.', 'Next sentence.']),
    ([' very', ' very', ' important.', ' Very', ' very', ' important.'],
     ['very very important.', 'Very very important.']),
    ([' The', ' U.S.', ' report', ' by', ' A.', ' Smith', ' arrived.'],
     ['The U.S. report by A. Smith arrived.']),
    ([' A deposit is refundable', ' only if cancellation is received', ' at least 14 days before the course begins.'],
     ['A deposit is refundable only if cancellation is received at least 14 days before the course begins.']),
])
def test_chunk_boundaries_preserve_sentences_and_real_repetitions(pieces, expected):
    pending, output = [], []
    for i, piece in enumerate(pieces):
        complete, pending = take_segments(pending + [token(piece, i*.1, (i+1)*.1)])
        output.extend(s['english'] for s in complete)
    complete, pending = take_segments(pending, final=True)
    output.extend(s['english'] for s in complete)
    assert output == expected and not pending


def test_pause_and_length_boundaries_are_explicit_fragments():
    result, pending = take_segments([token('An unfinished thought', 0, 1), token('After a pause', 5, 6)])
    assert result[0]['boundary'] == 'pause'
    assert pending[0]['text'] == 'After a pause'
    result, pending = take_segments([token('A very long fragment', 0, 35)])
    assert result[0]['boundary'] == 'length_limit' and not pending


def new_store(tmp_path):
    store = SessionStore(tmp_path)
    store.initialize(translation_model='hy-mt2:1.8b-q8', endpoint='http://127.0.0.1:11434')
    return store


def test_english_and_job_commit_atomically_before_translation(tmp_path):
    store = new_store(tmp_path)
    event = [token('The course is worth three credits.')]
    store.ingest(0, event, final=True)
    store.close()
    reopened = SessionStore(tmp_path)
    snap = reopened.snapshot()
    assert snap['segments'][0]['english'] == event[0]['text']
    assert snap['segments'][0]['translation_state'] == 'pending'
    assert snap['segments'][0]['chinese'] is None
    assert reopened.ingest(0, event, final=True) == []
    assert len(reopened.snapshot()['segments']) == 1
    with pytest.raises(ValueError, match='冲突'):
        reopened.ingest(0, [token('Different text')], final=True)
    reopened.close()


def test_transaction_rollback_keeps_no_half_created_event_or_job(tmp_path, monkeypatch):
    store = new_store(tmp_path)
    def fail(*args):
        raise sqlite3.OperationalError('simulated disk failure')
    monkeypatch.setattr(store, '_add_segment', fail)
    with pytest.raises(sqlite3.OperationalError):
        store.ingest(0, [token('Must survive.')], final=True)
    assert store.db.execute('SELECT COUNT(*) FROM asr_events').fetchone()[0] == 0
    assert store.db.execute('SELECT COUNT(*) FROM segments').fetchone()[0] == 0
    store.close()


def test_pending_confirmed_words_survive_reopen_and_eof(tmp_path):
    store = new_store(tmp_path)
    store.ingest(0, [token('This has no final punctuation')])
    store.close()
    store = SessionStore(tmp_path)
    assert store.snapshot()['pending_english'] == 'This has no final punctuation'
    store.ingest(1, [], final=True)
    assert store.snapshot()['segments'][0]['english'] == 'This has no final punctuation'
    assert not store.snapshot()['pending_english']
    store.close()


def test_translation_failure_retry_and_old_lease_cannot_overwrite(tmp_path):
    store = new_store(tmp_path)
    store.ingest(0, [token('Original English.')], final=True)
    old = store.claim()
    assert store.finish(old, error='server unavailable')
    assert store.snapshot()['segments'][0]['english'] == 'Original English.'
    store.recover(retry_failed=True)
    current = store.claim()
    assert not store.finish(old, result={'translation': '过期结果'})
    assert store.finish(current, result={'translation': '有效结果'})
    assert store.claim() is None
    assert store.snapshot()['segments'][0]['attempts'] == 2
    store.close()


def test_source_revision_invalidates_inflight_translation(tmp_path):
    store = new_store(tmp_path)
    store.ingest(0, [token('At most 14 days.')], final=True)
    stale = store.claim()
    store.revise(1, 'At least 14 days.')
    assert not store.finish(stale, result={'translation': '至多十四天'})
    new = store.claim()
    assert new['revision'] == 2
    assert store.finish(new, result={'translation': '至少十四天'})
    assert store.db.execute('SELECT COUNT(*) FROM source_revisions').fetchone()[0] == 2
    store.close()


def test_running_job_recovery_does_not_redo_completed_work(tmp_path):
    store = new_store(tmp_path)
    store.ingest(0, [token('First.'), token(' Second.')], final=True)
    first = store.claim(); store.finish(first, result={'translation': '第一句'})
    second = store.claim(); store.close()
    store = SessionStore(tmp_path); store.recover()
    recovered = store.claim()
    assert recovered['id'] == second['id'] and recovered['lease'] != second['lease']
    assert store.snapshot()['segments'][0]['translation_state'] == 'completed'
    store.close()


def test_subtitle_time_carry_overlap_and_markup():
    snap = {'segments': [
        {'start': 59.9996, 'end': 61, 'english': '<b>Hello</b>', 'chinese': '你好'},
        {'start': 60.8, 'end': 61, 'english': 'Next\n\nline', 'chinese': None},
    ]}
    srt = export_subtitles(snap, 'srt')
    assert '00:01:00,000 --> 00:01:01,000' in srt
    assert '00:01:01,000 --> 00:01:01,100' in srt
    assert '&lt;b&gt;Hello&lt;/b&gt;' in srt and '［尚未翻译］' in srt
    assert export_subtitles(snap, 'vtt').startswith('WEBVTT\n')


@pytest.mark.parametrize('endpoint', ['https://127.0.0.1:11434', 'http://localhost:11434',
    'http://example.com', 'http://user@127.0.0.1', 'http://127.0.0.1/path', 'http://127.0.0.1/?x=1'])
def test_remote_or_ambiguous_endpoints_rejected(endpoint):
    with pytest.raises(ValueError):
        validate_endpoint(endpoint)


def test_truncated_translation_never_returns_success():
    async def run():
        client = LocalTranslator('http://127.0.0.1:11434', 'local')
        async def handler(request):
            if request.url.path == '/api/tags':
                return httpx.Response(200, json={'models': [{'name': 'local', 'digest': 'abc'}]})
            if request.url.path == '/api/show':
                return httpx.Response(200, json={'capabilities': ['completion']})
            return httpx.Response(200, text=json.dumps({'message': {'content': '半句译文'}})+'\n')
        await client.client.aclose()
        client.client = httpx.AsyncClient(base_url=client.endpoint, transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ValueError, match='中断'):
                await client.translate('Original sentence.')
        finally:
            await client.close()
    asyncio.run(run())
