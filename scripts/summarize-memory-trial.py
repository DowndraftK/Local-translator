#!/usr/bin/env python3
"""Extract compact memory evidence without publishing task text or raw logs."""
import argparse
import json
from pathlib import Path


def summarize(root):
    root = Path(root)
    samples = [json.loads(line) for line in (root/'samples.jsonl').read_text().splitlines()]
    operations = json.loads((root/'operations.json').read_text())
    conditions = json.loads((root/'conditions.json').read_text())
    result = {'conditions': conditions, 'sample_count': len(samples), 'stages': {}, 'operations': []}
    for stage in dict.fromkeys(s['stage'] for s in samples):
        selected = [s for s in samples if s['stage'] == stage]
        processes = {}
        for value in selected:
            for process in value['processes']:
                pid = str(process['pid'])
                name = Path(process['command'].split()[0]).name
                entry = processes.setdefault(pid, {'name': name, 'first_rss_bytes': process['rss_bytes'],
                    'sampled_peak_rss_bytes': 0, 'last_rss_bytes': None, 'samples': 0})
                entry['sampled_peak_rss_bytes'] = max(entry['sampled_peak_rss_bytes'], process['rss_bytes'])
                entry['last_rss_bytes'] = process['rss_bytes']
                entry['samples'] += 1
        result['stages'][stage] = {'seconds_between_first_last_sample': selected[-1]['time']-selected[0]['time'],
            'processes': processes, 'first_ollama': selected[0]['ollama'], 'last_ollama': selected[-1]['ollama'],
            'first_system': selected[0]['system'], 'last_system': selected[-1]['system']}
    for operation in operations:
        snapshot = operation.get('snapshot', {})
        value = {k:v for k,v in operation.items() if k != 'snapshot'}
        value.update({k:snapshot.get(k) for k in ['state', 'asr_complete', 'segment_count',
            'translation_counts', 'worker_rss_bytes', 'worker_peak_rss_bytes', 'load_seconds',
            'device_evidence', 'asr_tail_seconds', 'speech_release', 'encoder_lifecycle', 'audio_sha256', 'resources_digest']})
        value['recipe'] = snapshot.get('translation_configuration')
        progress = root/operation['name']/'progress.jsonl'
        if progress.is_file():
            rows = [json.loads(line) for line in progress.read_text().splitlines()]
            after = [r for r in rows if r.get('state') == 'translating'
                     and r.get('translation_counts', {}).get('pending', 0) > 0
                     and r.get('translation_counts', {}).get('running', 0) == 0]
            if after:
                stable = [r for r in after if r['time'] >= after[0]['time']+5]
                value['after_speech_before_translation'] = {k:v for k,v in (stable[-1] if stable else after[-1]).items()
                    if k in ['time','worker_rss_bytes','worker_peak_rss_bytes','state','translation_counts']}
        observed = value.get('after_speech_before_translation')
        if observed and observed.get('worker_rss_bytes') is None:
            pid = snapshot.get('worker_pid')
            # Match the already established >=5s post-release window to the
            # parent observer when the worker's own ps is denied by isolation.
            candidates = [r for r in samples if r['stage'] == operation['name'] and observed['time'] - 5 <= r['time'] <= observed['time']]
            readings = [p for r in candidates for p in r['processes'] if p['pid'] == pid]
            if readings:
                observed['worker_rss_bytes'] = readings[-1]['rss_bytes']
                observed['rss_source'] = 'external 5-second observer; worker-local ps unavailable under isolation'
        result['operations'].append(value)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(summarize(args.root), ensure_ascii=False, indent=2)+'\n')
