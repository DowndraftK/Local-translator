"""Exercise the microphone worker transport with a file, without opening a mic."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--session', type=Path, required=True)
    args = parser.parse_args()
    args.session.mkdir(parents=True, exist_ok=False)
    with wave.open(str(args.input), 'rb') as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        expected = audio.readframes(audio.getnframes())
    with (args.session/'transport.log').open('w') as log:
        child = subprocess.Popen([sys.executable, '-m', 'streaming_translator', 'run',
            '--config', str(args.config), '--session', str(args.session), '--stdin-pcm', '--no-translation'],
            stdin=subprocess.PIPE, stdout=log, stderr=log, env=os.environ.copy())
        try:
            deadline = time.monotonic()+120
            while True:
                if child.poll() is not None:
                    raise RuntimeError('Worker exited before ready')
                snap_path = args.session/'snapshot.json'
                if snap_path.exists() and json.loads(snap_path.read_text()).get('state') == 'recognizing':
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError('Model readiness timeout')
                time.sleep(.1)
            start, offset = time.monotonic(), 0
            # Irregular transport writes, including a final short PCM packet.
            sizes = [3198, 6402, 16000, 258]
            count = 0
            while offset < len(expected):
                data = expected[offset:offset+sizes[count % len(sizes)]]
                child.stdin.write(data); child.stdin.flush()
                offset += len(data); count += 1
                time.sleep(max(0, offset/32000-(time.monotonic()-start)))
            child.stdin.close()
            code = child.wait(timeout=180)
            assert code == 0, f'Worker failed: {code}'
        finally:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=30)
                except subprocess.TimeoutExpired: child.kill(); child.wait()
    with wave.open(str(args.session/'audio.wav'), 'rb') as saved:
        actual = saved.readframes(saved.getnframes())
    snapshot = json.loads((args.session/'snapshot.json').read_text())
    assert actual == expected, 'Archived PCM differs from input'
    assert snapshot['received_pcm_samples'] == len(expected)//2
    assert snapshot['asr_complete'] and snapshot['state'] == 'completed'
    assert not snapshot['pending_english']
    report = {'state': snapshot['state'], 'samples': len(expected)//2,
        'exact_pcm_match': True, 'pcm_sha256': hashlib.sha256(actual).hexdigest(),
        'last_transport_write_bytes': len(data), 'segments': len(snapshot['segments']),
        'scope': 'stdin transport, archive and EOF; not a physical microphone test'}
    (args.session/'transport-check.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
