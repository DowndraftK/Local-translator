import json
import math

import numpy as np
import pytest

from streaming_translator.store import SessionStore, export_subtitles
from streaming_translator.timing import map_token
from streaming_translator.vad import LookbackVAD


def session(tmp_path):
    store = SessionStore(tmp_path)
    store.initialize(translation_model='fixture', audio_seconds=20)
    return store


def test_time_origin_applied_once_and_raw_local_evidence_kept(tmp_path):
    store = session(tmp_path)
    t = map_token('After a pause.', 1.25, 2.5, 160000, 240000)
    store.ingest(0, [t], final=True)
    row = store.snapshot()['segments'][0]
    assert (row['start'], row['end']) == (11.25, 12.5)
    evidence = json.loads(store.db.execute('SELECT evidence FROM segment_timing').fetchone()[0])
    assert evidence['raw_tokens'][0]['local']['origin_sample'] == 160000
    assert evidence['raw_tokens'][0]['estimate'] == {'start': 11.25, 'end': 12.5}
    store.close()


def test_same_model_alignment_preserves_raw_estimate_and_applies_resume_origin_once(tmp_path):
    store = session(tmp_path)
    alignment = {'raw_start': 0.1, 'raw_end': 0.3, 'method': 'same_model_attention_dtw',
                 'window_start': 0, 'window_end': 4}
    token = map_token('After a pause.', 2, 3, 160000, 240000, alignment=alignment)
    store.ingest(0, [token], final=True)
    row = store.snapshot()['segments'][0]
    assert (row['start'], row['end']) == (12, 13)
    evidence = json.loads(store.db.execute('SELECT evidence FROM segment_timing').fetchone()[0])
    assert evidence['raw_tokens'][0]['estimate'] == {'start': 10.1, 'end': 10.3}
    assert evidence['raw_tokens'][0]['alignment']['global_window_start'] == 10
    assert evidence['raw_tokens'][0]['alignment']['global_window_end'] == 14
    assert 'same_model_attention_alignment' in evidence['reasons']
    for kind in ('txt', 'srt', 'vtt'):
        assert 'After a pause.' in export_subtitles(store.snapshot(), kind)
    store.close()


@pytest.mark.parametrize('start,end', [(float('nan'), 5), (-1, 3), (6, 2), (19, 40), (20, 20), (float('inf'), None)])
def test_invalid_estimate_keeps_words_raw_evidence_and_bounded_exports(tmp_path, start, end):
    store = session(tmp_path)
    store.ingest(0, [{'text': 'Do not lose 3.14.', 'start': start, 'end': end}], final=True)
    row = store.snapshot()['segments'][0]
    assert row['english'] == 'Do not lose 3.14.'
    assert 0 <= row['start'] < row['end'] <= 20
    assert row['timing_status'] == 'needs_review'
    assert store.db.execute('SELECT payload FROM asr_events').fetchone()[0]
    for kind in ('srt', 'vtt', 'txt'):
        exported = export_subtitles(store.snapshot(), kind)
        assert 'Do not lose 3.14.' in exported and 'nan' not in exported.lower()
    store.close()


def test_punctuation_extent_does_not_expand_spoken_sentence(tmp_path):
    store = session(tmp_path)
    store.ingest(0, [{'text': ' First', 'start': 2, 'end': 3},
                     {'text': '.', 'start': 0, 'end': 18}], final=True)
    row = store.snapshot()['segments'][0]
    assert (row['start'], row['end']) == (2, 3)
    evidence = json.loads(store.db.execute('SELECT evidence FROM segment_timing').fetchone()[0])
    assert evidence['reasons'] == ['punctuation_has_no_speech_extent']
    store.close()


def test_overlap_and_repetition_do_not_cascade_or_delete_words(tmp_path):
    store = session(tmp_path)
    for i in range(250):
        store.ingest(i, [{'text': 'No, no. 3.14.', 'start': 10, 'end': 11}], final=True)
    snap = store.snapshot()
    assert len(snap['segments']) == 250
    assert all(s['start'] == 10 and s['end'] == 11 for s in snap['segments'])
    assert export_subtitles(snap).count('00:00:10,000 --> 00:00:11,000') == 250
    assert export_subtitles(snap, 'vtt').count('00:00:10.000 --> 00:00:11.000') == 250
    assert export_subtitles(snap, 'txt').count('[10.00–11.00]') == 250
    store.close()


def test_timing_only_update_preserves_source_revision_and_translation_lease(tmp_path):
    store = session(tmp_path)
    store.ingest(0, [{'text': 'Yes.', 'start': 1, 'end': 2}], final=True)
    job = store.claim()
    store.retime(1, 32000, 48000, 'Fixture waveform boundaries')
    assert store.finish(job, result={'translation': '是。'})
    row = store.snapshot()['segments'][0]
    assert row['revision'] == 1 and row['chinese'] == '是。' and row['start'] == 2
    assert row['timing_status'] == 'corrected'
    store.revise(1, 'No.')
    assert store.snapshot()['segments'][0]['chinese'] is None
    assert not store.finish(job, result={'translation': '过期'})
    store.close()


def test_legacy_migration_does_not_invent_original_estimates(tmp_path):
    store = session(tmp_path)
    store.ingest(0, [{'text': 'Legacy.', 'start': 1, 'end': 2}], final=True)
    store.db.execute('DROP TABLE segment_timing')
    store.db.execute('PRAGMA user_version=2')
    store.close()
    store = SessionStore(tmp_path)
    row = store.snapshot()['segments'][0]
    assert row['timing_status'] == 'unknown' and row['english'] == 'Legacy.'
    assert store.db.execute('SELECT COUNT(*) FROM segment_timing').fetchone()[0] == 0
    assert store.db.execute('PRAGMA user_version').fetchone()[0] == 3
    store.close()


def test_vad_duplicate_onset_does_not_erase_buffered_speech():
    gate = LookbackVAD(preroll=0, postroll=0, decision_delay=100)
    audio = np.arange(90, dtype=np.float32)
    spans = gate.feed(audio, [{'start': 5}, {'start': 30}, {'end': 70}], final=True)
    assert [(a, b) for a,b,active,_ in spans if active] == [(5, 70)]
    assert np.array_equal(np.concatenate([x[3] for x in spans]), audio)


def test_vad_late_event_accounts_for_irrecoverable_prefix_without_resending():
    gate = LookbackVAD(preroll=0, postroll=0, decision_delay=10)
    first = gate.feed(np.arange(100, dtype=np.float32))
    rest = gate.feed(np.arange(100,120,dtype=np.float32), [{'start': 50}], final=True)
    assert gate.late_events == 1
    assert np.array_equal(np.concatenate([x[3] for x in first+rest]), np.arange(120))
    assert [(a,b) for a,b,active,_ in rest if active] == [(90,120)]


def test_empty_output_warning_distinguishes_vad_silence_and_stays_bounded():
    from streaming_translator.quality import OutputWatch
    watch = OutputWatch()
    for i in range(100):
        watch.span(i, i+1, False)
        assert watch.observe(i+1, False) is None
    issues = []
    for i in range(100, 1000):
        watch.span(i, i+1, True)
        issue = watch.observe(i+1, False)
        if issue: issues.append(issue)
        assert len(watch.spans) < 63
    assert len(issues) == 1 and issues[0]['active_seconds'] == 10
    watch.observe(1000, True)
    for i in range(1000, 1010): watch.span(i, i+1, True)
    assert watch.observe(1010, False)['active_seconds'] == 10


def test_integrity_check_rejects_unrecorded_patch_before_loading_models(tmp_path):
    from streaming_translator.asr import validate_resources
    import hashlib
    source = tmp_path/'source'; source.mkdir()
    script = source/'align.py'; script.write_text('original')
    manifest = tmp_path/'source.json'
    manifest.write_text(json.dumps({'patched_files': {'align.py': hashlib.sha256(script.read_bytes()).hexdigest()}}))
    script.write_text('unrecorded change')
    with pytest.raises(ValueError, match='源码校验失败'):
        validate_resources({'source': str(source), 'source_manifest': str(manifest)})


def test_resume_archives_timing_and_tail_warnings_without_polluting_replay(tmp_path):
    store = session(tmp_path)
    store.ingest(0, [{'text': 'Prefix.', 'start': 1, 'end': 2}], final=True, complete=False)
    store.checkpoint(48000, 'media', 'resources')
    checkpoint = store.latest_checkpoint()
    store.ingest(1, [{'text': 'Tail.', 'start': 6, 'end': 7}], final=True, complete=False)
    store.quality_issue({'kind': 'no_committed_words', 'start': 8, 'end': 18})
    store.restart_from_checkpoint(checkpoint)
    assert store.snapshot()['quality_issue_count'] == 0
    history = json.loads(store.db.execute('SELECT payload FROM recovery_history').fetchone()[0])
    assert len(history['segment_timing']) == 1
    assert history['metadata']['quality_issue_count'] == 1
    assert len(store.snapshot()['segments']) == 1
    store.close()


def test_long_vad_silence_without_any_inference_does_not_accumulate_spans():
    from streaming_translator.quality import OutputWatch
    watch = OutputWatch()
    for index in range(7200):
        watch.span(index/2, (index+1)/2, False)
    assert len(watch.spans) == 1
    assert watch.observe(3600, False) is None


def test_confirmed_vad_pause_flushes_unpunctuated_words_without_completing_task(tmp_path):
    store = session(tmp_path)
    store.ingest(0, [{'text': 'No no fourteen', 'start': 1, 'end': 2}])
    store.ingest(1, [], final=True, complete=False, boundary='vad_pause')
    assert not store.get('asr_complete')
    assert store.snapshot()['segments'][0]['boundary'] == 'vad_pause'
    store.ingest(2, [{'text': 'No no fourteen', 'start': 3, 'end': 4}], final=True)
    assert [r['english'] for r in store.snapshot()['segments']] == ['No no fourteen']*2
    store.close()
