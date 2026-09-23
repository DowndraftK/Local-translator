"""Real-model pause/pipe and SIGKILL/resume checks, using only owned test workers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import wave


def wait_for(check, timeout=120):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.1)
    raise TimeoutError('Expected test state was not reached')


def snapshot(directory):
    try:
        return json.loads((directory/'snapshot.json').read_text())
    except (OSError, ValueError):
        return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--after-endurance', type=Path)
    args = parser.parse_args()
    if args.after_endurance:
        def finished():
            try:
                value = json.loads(args.after_endurance.read_text())
                return value if 'exit_code' in value else None
            except (OSError, ValueError):
                return None
        prior = wait_for(finished, timeout=7200)
        assert prior['exit_code'] == 0, prior
    args.output.mkdir(parents=True, exist_ok=False)
    with wave.open(str(args.input), 'rb') as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 16000)
        pcm = source.readframes(45*16000)
    fixture = args.output/'speech-45s.wav'
    with wave.open(str(fixture), 'wb') as target:
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(16000)
        target.writeframes(pcm)
    report = {'started_at': time.time(), 'samples': len(pcm)//2,
              'pcm_sha256': hashlib.sha256(pcm).hexdigest(),
              'runtime_files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in Path('runtime/streaming_translator').glob('*.py')}}
    base = [sys.executable, '-m', 'streaming_translator']
    child = None
    try:
        session = args.output/'pipe-pause'
        session.mkdir()
        events = [{'id': 'pause-fixture', 'kind': 'paused', 'sample': 15*16000},
                  {'id': 'resume-fixture', 'kind': 'resumed', 'sample': 15*16000}]
        (session/'capture-events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
        with (args.output/'pipe.log').open('w') as log:
            child = subprocess.Popen(base+['run', '--session', str(session), '--config', str(args.config),
                '--stdin-pcm', '--no-translation'], stdin=subprocess.PIPE, stdout=log, stderr=log)
            wait_for(lambda: snapshot(session).get('state') == 'recognizing')
            started = time.monotonic()
            for offset in range(0, len(pcm), 997):
                child.stdin.write(pcm[offset:offset+997])
            child.stdin.flush()
            wait_for(lambda: snapshot(session).get('durable_audio_samples') == len(pcm)//2)
            archived = snapshot(session)
            report['pipe_archive_seconds'] = time.monotonic()-started
            report['asr_processed_when_all_audio_archived'] = archived.get('processed_audio_seconds', 0)
            assert not archived.get('asr_complete')
            child.stdin.close()
            assert child.wait(timeout=240) == 0
        final = snapshot(session)
        assert final['state'] == 'completed' and final['received_pcm_samples'] == len(pcm)//2
        with wave.open(str(session/'audio.wav'), 'rb') as media:
            assert media.readframes(media.getnframes()) == pcm
        assert final['checkpoint_sample'] >= 15*16000
        report['pipe_final'] = {k: final.get(k) for k in ('state', 'received_pcm_samples', 'asr_backlog_seconds', 'checkpoint_sample', 'segment_count')}

        session = args.output/'crash-resume'
        with (args.output/'before-crash.log').open('w') as log:
            child = subprocess.Popen(base+['run', '--session', str(session), '--config', str(args.config),
                '--input', str(fixture), '--paced', '--no-translation'], stdout=log, stderr=log)
            wait_for(lambda: snapshot(session).get('checkpoint_sample', 0) > 0)
            checkpoint = snapshot(session)['checkpoint_sample']
            wait_for(lambda: snapshot(session).get('received_audio_seconds', 0) > checkpoint/16000+4)
            child.kill(); child.wait(timeout=10)
        with sqlite3.connect(session/'session.sqlite') as db:
            point = db.execute('SELECT sample,segment_id FROM checkpoints ORDER BY sample DESC LIMIT 1').fetchone()
            prefix = db.execute('SELECT id,revision,start,end,english FROM segments WHERE id<=? ORDER BY id', (point[1],)).fetchall()
        before = snapshot(session)
        with (args.output/'after-resume.log').open('w') as log:
            child = subprocess.Popen(base+['resume', '--session', str(session)], stdout=log, stderr=log)
            assert child.wait(timeout=240) == 0
        final = snapshot(session)
        assert final['state'] == 'completed' and final['asr_complete']
        assert final['received_pcm_samples'] == final['input_samples'] == len(pcm)//2
        assert final['resume_sample'] == point[0]
        with sqlite3.connect(session/'session.sqlite') as db:
            assert db.execute('SELECT id,revision,start,end,english FROM segments WHERE id<=? ORDER BY id', (point[1],)).fetchall() == prefix
            assert db.execute('SELECT COUNT(*) FROM recovery_history').fetchone()[0] == 1
            sequences = [r[0] for r in db.execute('SELECT sequence FROM asr_events ORDER BY sequence')]
            assert sequences == list(range(len(sequences)))
        report['recovery'] = {'checkpoint_sample': point[0], 'preserved_segments': len(prefix),
            'before_received_seconds': before.get('received_audio_seconds'),
            'final_state': final['state'], 'final_received_samples': final['received_pcm_samples'],
            'final_segments': final['segment_count'], 'event_sequence_contiguous': True}
        report.update(finished_at=time.time(), passed=True)
    except BaseException as exc:
        report.update(finished_at=time.time(), passed=False, error=repr(exc))
        raise
    finally:
        if child and child.poll() is None:
            child.kill(); child.wait(timeout=10)
        (args.output/'summary.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__':
    main()
