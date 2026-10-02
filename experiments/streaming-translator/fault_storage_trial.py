"""Explicit opt-in ENOSPC trial on a mounted, disposable volume <=64 MiB.

No microphone, GPU or model calls. --runtime may point to a packaged runtime.
Keeps session evidence; deletes only the filler created by this invocation.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import wave


def fill_volume(root):
    filler = root / ('fault-filler-' + uuid.uuid4().hex)
    with filler.open('xb', buffering=0) as stream:
        for size in (65536, 4096, 512):
            try:
                while True:
                    stream.write(bytes(size))
            except OSError as exc:
                if exc.errno != errno.ENOSPC:
                    raise
    assert os.statvfs(root).f_bavail == 0, 'Volume did not reach actual ENOSPC'
    return filler


def read_pcm(path):
    with wave.open(str(path), 'rb') as media:
        frames = media.getnframes()
        data = media.readframes(frames)
        assert len(data) == frames * 2
        return frames, data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--volume', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.volume.resolve()
    stat = os.statvfs(root)
    assert os.stat(root).st_dev != os.stat(root.parent).st_dev, 'Must be a separate mounted volume'
    assert stat.f_blocks * stat.f_frsize <= 64 * 1024 * 1024, 'Volume is too large'
    sys.path.insert(0, str(args.runtime.resolve()))
    from streaming_translator.store import SessionStore
    trial = root / ('trial-' + uuid.uuid4().hex)
    trial.mkdir()
    report = {'volume_bytes': stat.f_blocks * stat.f_frsize, 'actual_enospc': True}
    store = SessionStore(trial / 'database')
    store.initialize(translation_model=None)
    store.ingest(0, [{'text': 'Saved prefix.', 'start': 0, 'end': 1}], final=True)
    before = [tuple(row) for row in store.db.execute('SELECT * FROM segments')]
    filler = fill_volume(root)
    try:
        try:
            store.ingest(1, [{'text': 'x' * 200000, 'start': 1, 'end': 2}], final=True)
            raise AssertionError('Full database accepted a large new transaction')
        except __import__('sqlite3').OperationalError as exc:
            assert 'full' in str(exc).lower()
            report['database_error'] = str(exc)
    finally:
        filler.unlink()
    assert [tuple(row) for row in store.db.execute('SELECT * FROM segments')] == before
    assert store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    report['database_committed_prefix_preserved'] = True
    store.close()

    config = trial / 'config.json'
    config.write_text('{}')
    env = dict(os.environ, PYTHONPATH=str(args.runtime.resolve()), PYTHONDONTWRITEBYTECODE='1')
    for scenario in ('wave', 'state'):
        session = trial / scenario
        log = trial / (scenario + '.log')
        payload = bytes(range(256)) * 125
        with log.open('w') as output:
            child = subprocess.Popen([sys.executable, '-m', 'streaming_translator', 'record',
                '--config', str(config), '--session', str(session), '--no-translation'],
                stdin=subprocess.PIPE, stdout=output, stderr=output, env=env)
            filler = None
            try:
                child.stdin.write(payload)
                child.stdin.flush()
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    try:
                        if json.loads((session / 'snapshot.json').read_text()).get('durable_audio_samples') == len(payload)//2:
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(.02)
                else:
                    raise AssertionError('No durable initial capture boundary')
                # Let the next half-second WAV write exceed the remaining space.
                # State-only leaves the recorder idle; the publisher must stop it.
                filler = fill_volume(root)
                triggered = time.monotonic()
                if scenario == 'wave':
                    child.stdin.write(bytes(16000))
                    child.stdin.flush()
                child.wait(timeout=5)
                assert child.returncode != 0, 'Storage failure falsely succeeded'
                stopped_after = time.monotonic() - triggered
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                child.stdin.close()
                if filler is not None:
                    filler.unlink()
        frames, pcm = read_pcm(session / 'audio.wav')
        assert pcm[:len(payload)] == payload
        saved = SessionStore(session)
        assert saved.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        report[scenario] = {'exit': child.returncode, 'stopped_after_seconds': stopped_after,
            'durable_before_samples': len(payload)//2, 'readable_after_samples': frames,
            'saved_prefix_sha256': hashlib.sha256(payload).hexdigest(), 'prefix_preserved': True,
            'durable_metadata_samples': saved.get('durable_audio_samples')}
        saved.close()
    report['evidence_directory'] = str(trial)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
