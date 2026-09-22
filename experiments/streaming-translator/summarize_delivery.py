"""Produce compact, transcript-free evidence from the completed delivery trials."""
import argparse
import hashlib
import json
from pathlib import Path

from summarize_ted import summarize, word_errors, words


def fingerprint(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def compact_comparison(reference, text):
    score = word_errors(reference, words(text))
    score.pop('operations')
    return score


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--stream-session', type=Path, action='append', required=True)
    parser.add_argument('--refined-session', type=Path, required=True)
    parser.add_argument('--offline-result', type=Path, required=True)
    parser.add_argument('--pcm-session', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reference = words(args.reference.read_text())
    streams = []
    for path in args.stream_session:
        trial = summarize(path, reference)
        trial['word_comparison'].pop('operations')
        snapshot = json.loads((path/'snapshot.json').read_text())
        trial['max_context_tokens'] = snapshot['asr_config']['max_context_tokens']
        streams.append(trial)
    refined = json.loads((args.refined_session/'snapshot.json').read_text())
    assert refined['state'] == 'completed' and refined['asr_complete']
    refinement = {k: refined.get(k) for k in ['session_id', 'parent_session_id',
        'state', 'audio_seconds', 'input_samples', 'received_pcm_samples',
        'audio_sha256', 'refinement_seconds', 'last_operation_seconds',
        'device_evidence', 'translation_counts']}
    refinement.update(segments=len(refined['segments']),
        snapshot_sha256=fingerprint(args.refined_session/'snapshot.json'),
        word_comparison=compact_comparison(reference, ' '.join(s['english'] for s in refined['segments'])))
    offline = json.loads(args.offline_result.read_text())
    report = {'date': '2026-09-17', 'scope': 'One TED talk; not a classroom corpus or MT quality assessment',
        'source_url': 'https://www.ted.com/talks/celeste_headlee_10_ways_to_have_a_better_conversation',
        'reference_sha256': fingerprint(args.reference),
        'latency_limitations': 'Segment ends are ASR estimates. Baseline clock offset about 0.5s. App builds overlapped trials; no controlled hardware comparison. First translation includes cold service/model wait.',
        'streams': streams, 'native_refinement': refinement,
        'offline_diagnostic': {'elapsed_seconds': offline['elapsed_seconds'],
            'result_sha256': fingerprint(args.offline_result),
            'word_comparison': compact_comparison(reference, offline['result']['text'])},
        'stdin_pcm': json.loads((args.pcm_session/'transport-check.json').read_text()),
        'interrupted_trial': {'coalesce_seconds': 1, 'state_at_last_snapshot': 'finishing_asr',
            'completed_translations_at_last_snapshot': 146, 'excluded_from_complete_comparison': True}}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print('Written', args.output)


if __name__ == '__main__':
    main()
