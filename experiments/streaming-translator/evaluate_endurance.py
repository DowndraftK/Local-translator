"""Evaluate repeated speech with the established scoring and complete saved rows.

This reads the trial without changing it. The default report contains metrics,
not transcripts. Optional error details belong in a private artifacts directory.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import wave

from summarize_ted import lag_summary, word_errors, words
from streaming_translator.segmentation import take_segments


def deleted_run(operations):
    longest = count = 0
    previous = -2
    for op in operations:
        if op['type'] != 'deletion':
            continue
        index = op['reference_index']
        count = count + 1 if index == previous + 1 else 1
        longest = max(longest, count)
        previous = index
    return longest


def repeated_ngram(text):
    tokens = words(text)
    # A narrow screen, not a classifier: real emphasis can also repeat.
    for width in range(3, 13):
        for start in range(len(tokens) - 3 * width + 1):
            a = tokens[start:start + width]
            if a == tokens[start + width:start + 2 * width] == tokens[start + 2 * width:start + 3 * width]:
                return True
    return False


def evaluate(trial, reference, allow_incomplete=False):
    manifest = json.loads((trial / 'manifest.json').read_text())
    session = trial / 'session'
    # Metadata, rows and events must share one read snapshot even when an
    # explicitly requested intermediate observation runs beside the worker.
    with sqlite3.connect((session / 'session.sqlite').resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        snapshot = {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM metadata')}
        snapshot['segments'] = [dict(r) for r in db.execute('SELECT s.*,t.state AS translation_state,'
            't.chinese,t.completed_at AS translated_at FROM segments s JOIN translations t ON t.segment_id=s.id ORDER BY s.id')]
        states = Counter(r['translation_state'] for r in snapshot['segments'])
        snapshot['translation_counts'] = {key: states[key] for key in ['pending', 'running', 'completed', 'failed', 'disabled']}
        events = [(r['sequence'], json.loads(r['payload'])) for r in db.execute('SELECT * FROM asr_events ORDER BY sequence')]
        timing = [json.loads(r['evidence']) for r in db.execute('SELECT * FROM segment_timing')]
        database_checks = {
            'sqlite_integrity': db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok',
            'foreign_keys_valid': not db.execute('PRAGMA foreign_key_check').fetchall(),
            'translation_revision_mismatches': db.execute('SELECT COUNT(*) FROM translations t JOIN segments s '
                'ON t.segment_id=s.id WHERE t.revision!=s.revision').fetchone()[0],
        }
    complete = snapshot.get('state') == 'completed' and snapshot.get('asr_complete') is True
    if not complete and not allow_incomplete:
        raise ValueError('Trial is incomplete; do not report a completed quality assessment')
    with wave.open(manifest['source'], 'rb') as audio:
        pcm = audio.readframes(audio.getnframes())
        source_seconds = audio.getnframes() / audio.getframerate()
    assert hashlib.sha256(pcm).hexdigest() == manifest['source_sha256']
    reference = words(reference)
    segments = snapshot['segments']
    available = snapshot.get('processed_audio_seconds', 0)
    rounds = []
    details = []
    for index, start in enumerate(manifest['repeat_starts']):
        end = start + source_seconds + 2
        if end > min(manifest['samples'] / 16000, available):
            break
        rows = [r for r in segments if start <= r['start'] < end]
        score = word_errors(reference, words(' '.join(r['english'] for r in rows)))
        score['longest_consecutive_deleted_words'] = deleted_run(score['operations'])
        details.append({'repetition': index + 1, 'operations': score.pop('operations')})
        rounds.append({'repetition': index + 1, 'start': start, 'end': end,
                       'segments': len(rows), **score})
    counts = Counter()
    for result in rounds:
        counts.update(result['counts'])
    total_reference = sum(r['reference_words'] for r in rounds)
    event_text = ''.join(t['text'] for _, e in events for t in e['tokens'])
    saved_text = ' '.join(r['english'] for r in segments)
    saved_pending = ''.join(t['text'] for t in snapshot.get('pending_tokens', []))
    pending, replayed = [], []
    for _, event in events:
        batch, pending = take_segments(pending + event['tokens'], final=event['final'])
        replayed.extend(r['english'] for r in batch)
    # Segment boundaries can introduce whitespace inside a split decoder
    # word (e.g. a name). Record that scoring difference separately; exact
    # event replay and ordered characters prove what the store preserved.
    characters = lambda text: ''.join(c for c in text if c.isalnum())
    checks = {
        **database_checks,
        'event_sequences_contiguous': [seq for seq, _ in events] == list(range(len(events))),
        'original_events_match_saved_and_pending_characters': characters(event_text) == characters(saved_text + saved_pending),
        'replayed_segments_match_saved_rows': replayed == [r['english'] for r in segments],
        'replayed_pending_matches': ''.join(t['text'] for t in pending) == saved_pending,
        'event_word_spacing_differs': words(event_text) != words(saved_text + saved_pending),
        'timing_evidence_count': len(timing),
    }
    starts_rewound = [b['id'] for a, b in zip(segments, segments[1:]) if b['start'] < a['start']]
    overlaps = [b['id'] for a, b in zip(segments, segments[1:]) if b['start'] < a['end']]
    warnings = snapshot.get('quality_issues', [])
    start_clock = snapshot.get('playback_started_at', 0)
    performance = {k: snapshot.get(k) for k in ['asr_elapsed_seconds', 'asr_tail_seconds',
        'asr_call_seconds', 'asr_calls', 'queue_peak_seconds', 'load_seconds']}
    performance['estimated_english_end_lag'] = lag_summary([r['committed_at'] - start_clock - r['end'] for r in segments])
    performance['estimated_chinese_end_lag'] = lag_summary([r['translated_at'] - start_clock - r['end']
        for r in segments if r['translation_state'] == 'completed'])
    return {
        'scope': 'One TED repeated with two seconds of silence; not independent samples or classroom/MT acceptance.',
        'normalization': 'Unchanged summarize_ted.words/word_errors. Numeral and contraction spellings remain errors.',
        'round_assignment': 'Saved effective segment start within repeat intervals; timestamps are estimates, not speech truth.',
        'complete': complete, 'full_rounds_scored': len(rounds), 'rounds': rounds,
        'total_reference_words': total_reference, 'total_errors': sum(r['errors'] for r in rounds),
        'counts': dict(counts), 'word_error_rate': sum(r['errors'] for r in rounds) / total_reference if total_reference else None,
        'input_samples': snapshot.get('input_samples'), 'received_pcm_samples': snapshot.get('received_pcm_samples'),
        'translation_counts': snapshot['translation_counts'], 'checks': checks,
        'timing': {'effective_start_rewinds': starts_rewound, 'effective_overlaps': overlaps,
                   'needs_review': sum(bool(t.get('issues')) for t in timing)},
        'warnings': dict(Counter(w['kind'] for w in warnings)),
        'three_consecutive_ngram_screen': {
            'segments': [r['id'] for r in segments if repeated_ngram(r['english'])],
            'events': [seq for seq, e in events if repeated_ngram(''.join(t['text'] for t in e['tokens']))],
            'note': 'Zero does not rule out every loop; hits need acoustic review and may be real repetition.'},
        'performance': performance,
        'latency_note': 'Submission lags use model-estimated ends; they are not independent acoustic latency measurements.',
    }, details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--details', type=Path, help='Private word-level error operations; never publish the full reference')
    parser.add_argument('--allow-incomplete', action='store_true', help='Intermediate observation only; complete remains false')
    args = parser.parse_args()
    report, details = evaluate(args.trial, args.reference.read_text(), args.allow_incomplete)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    if args.details:
        args.details.write_text(json.dumps(details, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ['complete', 'full_rounds_scored', 'counts', 'word_error_rate', 'checks']}))


if __name__ == '__main__':
    main()
