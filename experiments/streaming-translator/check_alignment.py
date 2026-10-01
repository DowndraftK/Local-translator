"""Compare independently frozen waveform anchors to complete saved subtitle rows."""
import argparse
from array import array
import json
from pathlib import Path
import statistics
import re

from summarize_ted import load_snapshot, words


def align(reference, hypothesis):
    rows = [array('H', range(len(hypothesis)+1))]
    for i, ref in enumerate(reference, 1):
        row = array('H', [i])
        for j, hyp in enumerate(hypothesis, 1):
            row.append(min(rows[-1][j]+1, row[-1]+1, rows[-1][j-1]+(ref != hyp)))
        rows.append(row)
    i,j = len(reference),len(hypothesis)
    matches = {}
    while i or j:
        if i and j and rows[i][j] == rows[i-1][j-1]+(reference[i-1]!=hypothesis[j-1]):
            if reference[i-1] == hypothesis[j-1]: matches[i-1] = j-1
            i-=1; j-=1
        elif i and rows[i][j] == rows[i-1][j]+1: i-=1
        else: j-=1
    return matches


def metrics(values):
    ordered = sorted(abs(v) for v in values)
    if not values: return {'n': 0}
    # Nearest rank P95, not interpolated away from a failed outlier.
    import math
    return {'n': len(values), 'signed_mean': statistics.mean(values),
        'signed_median': statistics.median(values), 'absolute_median': statistics.median(ordered),
        'absolute_p95': ordered[math.ceil(.95*len(ordered))-1], 'absolute_max': max(ordered)}


def timing_words(text, spoken_forms=False):
    if spoken_forms:
        # Auxiliary timing matching only. WER keeps its established strict
        # normalization; these spellings do not change the recorded ASR text.
        text = re.sub(r'\b3\.14\b', 'three point one four', text, flags=re.I)
        text = re.sub(r'\bDr\.?\b', 'Doctor', text, flags=re.I)
        text = re.sub(r'\b14\b', 'fourteen', text)
        text = re.sub(r'\b40\b', 'forty', text)
    return words(text)


def check(anchors, session, spoken_forms=False):
    snapshot = load_snapshot(session)
    if snapshot.get('state') != 'completed' or not snapshot.get('asr_complete'):
        raise ValueError(f'Trial is incomplete: {session} ({snapshot.get("state")})')
    reference, slices = [], []
    for anchor in anchors['anchors']:
        start = len(reference); reference += timing_words(anchor['text'], spoken_forms); slices.append((start,len(reference)))
    hypothesis, indices = [], []
    for row in snapshot['segments']:
        w = timing_words(row['english'], spoken_forms); hypothesis += w; indices += [row]*len(w)
    matched = align(reference, hypothesis)
    output = []
    for anchor, (start,end) in zip(anchors['anchors'], slices):
        row = {'id': anchor['id'], 'reference_start': anchor['start_sample']/anchors['rate'],
               'reference_end': anchor['end_sample']/anchors['rate'], 'uncertainty_seconds': anchor['uncertainty_seconds']}
        if start not in matched or end-1 not in matched:
            row['status'] = 'unmatched_boundary'
        else:
            first, last = indices[matched[start]],indices[matched[end-1]]
            row.update(status='matched', segment_ids=[first['id'],last['id']],
                effective_start=first['start'], effective_end=last['end'],
                start_error=first['start']-row['reference_start'], end_error=last['end']-row['reference_end'])
        output.append(row)
    usable = [r for r in output if r['status']=='matched']
    return {'scope': anchors['scope'], 'session': str(session), 'total_anchors': len(output),
        'matching': 'spoken/written 3.14, Dr, 14 and 40 equivalents' if spoken_forms else 'strict existing word normalization',
        'matched': len(usable), 'unmatched': [r['id'] for r in output if r['status']!='matched'],
        'start': metrics([r['start_error'] for r in usable]), 'end': metrics([r['end_error'] for r in usable]),
        'over_two_seconds': [r['id'] for r in usable if max(abs(r['start_error']),abs(r['end_error']))>2],
        'early_over_half_second': [r['id'] for r in usable if r['start_error'] < -.5], 'anchors': output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--anchors', type=Path, required=True)
    parser.add_argument('--session', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--spoken-forms', action='store_true', help='Supplemental timing match for spelled/spoken numeric forms; does not change WER')
    args = parser.parse_args()
    anchors = json.loads(args.anchors.read_text())
    reports = [check(anchors,s,args.spoken_forms) for s in args.session]
    args.output.write_text(json.dumps(reports,ensure_ascii=False,indent=2)+'\n')
    for report in reports: print(json.dumps({k:v for k,v in report.items() if k!='anchors'}))


if __name__ == '__main__': main()
