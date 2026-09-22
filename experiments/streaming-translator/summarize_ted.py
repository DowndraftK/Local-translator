"""Summarize whole-session TED trials without treating ASR timestamps as truth."""
import argparse
from array import array
from collections import Counter
import hashlib
import json
import re
import statistics
from pathlib import Path


def words(text):
    text = re.sub(r'\([^)]*\)', ' ', text)
    text = text.lower().replace('’', "'")
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text)


def word_errors(reference, hypothesis):
    rows = [array('H', range(len(hypothesis)+1))]
    for i, ref in enumerate(reference, 1):
        row = array('H', [i])
        for j, hyp in enumerate(hypothesis, 1):
            row.append(min(rows[-1][j]+1, row[-1]+1, rows[-1][j-1]+(ref != hyp)))
        rows.append(row)
    i, j = len(reference), len(hypothesis)
    operations = []
    while i or j:
        if i and j and rows[i][j] == rows[i-1][j-1] + (reference[i-1] != hypothesis[j-1]):
            if reference[i-1] != hypothesis[j-1]:
                operations.append({'type': 'substitution', 'reference_index': i-1, 'reference': reference[i-1], 'hypothesis': hypothesis[j-1]})
            i -= 1; j -= 1
        elif i and rows[i][j] == rows[i-1][j] + 1:
            operations.append({'type': 'deletion', 'reference_index': i-1, 'reference': reference[i-1]})
            i -= 1
        else:
            operations.append({'type': 'insertion', 'reference_index': i, 'hypothesis': hypothesis[j-1]})
            j -= 1
    return {'reference_words': len(reference), 'hypothesis_words': len(hypothesis),
            'word_error_rate': rows[-1][-1]/len(reference), 'errors': rows[-1][-1],
            'counts': dict(Counter(op['type'] for op in operations)), 'operations': list(reversed(operations))}


def percentile(values, fraction):
    values = sorted(values)
    return values[min(len(values)-1, int((len(values)-1)*fraction))] if values else None


def summarize(session, reference):
    snapshot = json.loads((session/'snapshot.json').read_text())
    if snapshot.get('state') != 'completed':
        raise ValueError(f'Trial incomplete: {session} ({snapshot.get("state")})')
    segments = snapshot['segments']
    hypothesis = words(' '.join(s['english'] for s in segments))
    start = snapshot['playback_started_at']
    english_lags = [s['committed_at']-start-s['end'] for s in segments]
    chinese_lags = [s['translated_at']-start-s['end'] for s in segments if s['translation_state'] == 'completed']
    translations = [s['translated_at']-s['committed_at'] for s in segments if s['translation_state'] == 'completed']
    summary = {k: snapshot.get(k) for k in ['session_id', 'state', 'audio_seconds', 'processed_audio_seconds',
        'input_samples', 'received_pcm_samples', 'load_seconds', 'asr_elapsed_seconds', 'input_finished_seconds',
        'asr_tail_seconds', 'asr_call_seconds', 'asr_calls', 'queue_peak_seconds',
        'first_committed_token_seconds', 'translation_counts', 'source_sha256', 'audio_sha256', 'device_evidence']}
    summary.update(session=str(session), segments=len(segments),
        coalesce_seconds=snapshot['asr_config']['asr_coalesce_min_s'],
        boundary_counts=dict(Counter(s['boundary'] for s in segments)),
        snapshot_sha256=hashlib.sha256((session/'snapshot.json').read_bytes()).hexdigest(),
        first_english_segment_seconds=min(s['committed_at'] for s in segments)-start,
        first_chinese_segment_seconds=min(s['translated_at'] for s in segments if s['chinese'])-start,
        word_comparison=word_errors(reference, hypothesis),
        estimated_english_end_lag={'median': statistics.median(english_lags), 'p95': percentile(english_lags, .95), 'max': max(english_lags)},
        estimated_chinese_end_lag={'median': statistics.median(chinese_lags), 'p95': percentile(chinese_lags, .95), 'max': max(chinese_lags)},
        translation_after_english={'median': statistics.median(translations), 'p95': percentile(translations, .95), 'max': max(translations)},
        all_input_received=snapshot['input_samples'] == snapshot['received_pcm_samples'],
        empty_pending_english=not snapshot.get('pending_english'))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--session', action='append', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reference = words(args.reference.read_text())
    report = {'scope': 'One publicly available TED recording with the official English transcript; not a classroom corpus or an MT quality score',
        'normalization': 'Lowercase; curly apostrophe normalized; parenthesized audience directions removed; punctuation discarded; numeral/spelled-number and contraction differences remain errors',
        'latency_note': 'End lags use model-estimated end times, not manually verified speech ends. The first 0.5-second trial saved playback_started_at about 0.5 seconds before the paced clock; treat its wall-clock latencies as approximate.',
        'reference_sha256': hashlib.sha256(args.reference.read_bytes()).hexdigest(),
        'trials': [summarize(s, reference) for s in args.session]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    for result in report['trials']:
        print(json.dumps({k: v for k, v in result.items() if k != 'word_comparison'}, ensure_ascii=False))
        print('WER', result['word_comparison']['word_error_rate'], result['word_comparison']['counts'])


if __name__ == '__main__':
    main()
