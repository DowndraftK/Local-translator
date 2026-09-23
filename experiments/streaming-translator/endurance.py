"""Repeat local speech to measure sustained pipeline load, not classroom quality.

Run with the runtime virtualenv and PYTHONPATH=runtime. Output stays local.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import wave


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--minutes', type=float, default=60)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    fixture = args.output/'repeated-speech.wav'
    with wave.open(str(args.input), 'rb') as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 16000)
        speech = source.readframes(source.getnframes())
    total = round(args.minutes*60*16000)
    starts, written = [], 0
    with wave.open(str(fixture), 'wb') as target:
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(16000)
        while written < total:
            starts.append(written/16000)
            chunk = (speech + bytes(2*16000*2))[:(total-written)*2]
            target.writeframes(chunk)
            written += len(chunk)//2
    manifest = {'fixture': 'repeated single-speaker TED plus 2s silence between repetitions',
                'samples': total, 'source': str(args.input.resolve()), 'repeat_starts': starts,
                'source_sha256': hashlib.sha256(speech).hexdigest(),
                'runtime_files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in Path('runtime/streaming_translator').glob('*.py')},
                'started_at': time.time()}
    (args.output/'manifest.json').write_text(json.dumps(manifest, indent=2))
    with (args.output/'worker.log').open('w') as log, (args.output/'system.jsonl').open('w') as metrics:
        child = subprocess.Popen([sys.executable, '-m', 'streaming_translator', 'run',
            '--config', str(args.config), '--input', str(fixture), '--session', str(args.output/'session'),
            '--paced'], stdout=log, stderr=log)
        try:
            while True:
                observation = {'time': time.time(), 'worker_pid': child.pid}
                for name, command in [('swap', ['/usr/sbin/sysctl', 'vm.swapusage']),
                                      ('vm_stat', ['/usr/bin/vm_stat']),
                                      ('thermal', ['/usr/bin/pmset', '-g', 'therm'])]:
                    try:
                        result = subprocess.run(command, capture_output=True, text=True, timeout=3)
                        observation[name] = result.stdout.strip() if result.returncode == 0 else None
                    except (OSError, subprocess.TimeoutExpired):
                        observation[name] = None
                result = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,rss=,comm='],
                                        capture_output=True, text=True, timeout=3)
                observation['model_processes'] = [line.strip() for line in result.stdout.splitlines()
                                                if 'ollama' in line.lower() or line.split()[0] == str(child.pid)]
                metrics.write(json.dumps(observation)+'\n'); metrics.flush()
                try:
                    code = child.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=180)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()
            raise
    manifest.update(finished_at=time.time(), exit_code=code)
    (args.output/'manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps({'exit_code': code, 'directory': str(args.output), 'elapsed': manifest['finished_at']-manifest['started_at']}))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
