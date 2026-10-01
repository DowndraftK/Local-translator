"""Score saved sessions with the established TED normalization, keeping errors local."""
import argparse
import json
from pathlib import Path
from summarize_ted import words, word_errors, load_snapshot, lag_summary


def score(session, reference):
    s = load_snapshot(session)
    if s.get('state') != 'completed' or not s.get('asr_complete'):
        raise ValueError(f'Trial is incomplete: {session} ({s.get("state")})')
    result = word_errors(words(reference), words(' '.join(x['english'] for x in s['segments'])))
    deletes = [x['reference_index'] for x in result['operations'] if x['type'] == 'deletion']
    longest = count = 0
    previous = -2
    for index in deletes:
        count = count+1 if index == previous+1 else 1
        longest = max(longest, count); previous = index
    result.update(longest_consecutive_deleted_words=longest)
    return {'session': str(session), 'state': s.get('state'), 'segments': len(s['segments']),
        'asr_complete': s.get('asr_complete'), 'word_comparison': result,
        'asr_call_seconds': s.get('asr_call_seconds'), 'asr_elapsed_seconds': s.get('asr_elapsed_seconds'),
        'queue_peak_seconds': s.get('queue_peak_seconds'), 'asr_tail_seconds': s.get('asr_tail_seconds'),
        'estimated_end_to_commit': lag_summary([x['committed_at']-s.get('playback_started_at',0)-x['end']
            for x in s['segments']])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--session', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reports = [score(p, args.reference.read_text()) for p in args.session]
    args.output.write_text(json.dumps(reports, ensure_ascii=False, indent=2)+'\n')
    for r in reports:
        print(Path(r['session']).name, {k:v for k,v in r['word_comparison'].items() if k != 'operations'})


if __name__ == '__main__': main()
