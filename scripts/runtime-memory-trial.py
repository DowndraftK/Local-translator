#!/usr/bin/env python3
"""Finite, serial GPU trial; raw evidence stays outside Git.

Uses the selected real App/runtime and one frozen short input. The test-only
translation gate lets ASR finish before Ollama uses the GPU, with a fixed 20s
observation window. Never unloads or stops a shared Ollama service.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.request

DRIVER = '''
import asyncio
import streaming_translator.__main__ as worker
original = worker.translation_loop
async def serial_translation(store, done, stop):
    await done.wait()
    await asyncio.sleep(20)
    return await original(store, done, stop)
worker.translation_loop = serial_translation
raise SystemExit(worker.main())
'''


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def api(name):
    try:
        with urllib.request.urlopen('http://127.0.0.1:11434/api/' + name, timeout=3) as r:
            return json.load(r)
    except Exception as exc:
        return {'error': str(exc)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--app', type=Path, required=True)
    p.add_argument('--python', type=Path, required=True)
    p.add_argument('--runtime', type=Path, required=True)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--idle-seconds', type=int, default=330)
    p.add_argument('--environment-root', type=Path)
    p.add_argument('--resource-root', type=Path)
    p.add_argument('--isolation-profile', type=Path)
    p.add_argument('--speech-only', action='store_true', help='Focused follow-up, requires the same resident 7B; no cold-text claims')
    args = p.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    env = {k: v for k, v in os.environ.items() if k not in (
        'PYTHONHOME', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'PYTHONPATH', 'DYLD_LIBRARY_PATH',
        'DYLD_FALLBACK_LIBRARY_PATH', 'PYTHONUSERBASE')}
    env.update(PYTHONPATH=str(args.runtime.resolve()), PYTHONDONTWRITEBYTECODE='1',
               PYTHONNOUSERSITE='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
               PYTORCH_ENABLE_MPS_FALLBACK='0')
    prefix = ['/usr/bin/sandbox-exec', '-f', str(args.isolation_profile.resolve())] if args.isolation_profile else []
    worker = None
    app = None
    stage = 'initial'
    finished = threading.Event()
    measurements = []
    operations = []
    initial = api('ps')
    (out/'conditions.json').write_text(json.dumps({
        'input_sha256': digest(args.input), 'config': json.loads(args.config.read_text()),
        'app': str(args.app.resolve()), 'runtime': str(args.runtime.resolve()),
        'python': str(args.python.resolve()), 'initial_ollama': initial,
        'ollama_version': api('version'), 'sampling_seconds': 5,
        'idle_seconds': args.idle_seconds, 'translation_gate_seconds': 20,
        'speech_only_followup': args.speech_only,
        'ownership': 'existing shared Ollama; no active unload or stop',
        'cache_conditions': 'OS cache not purged; unloaded model cold vs resident warm recorded',
        'rss_note': 'Per-process current RSS and discrete sampled maxima; no physical-memory sum.'
    }, ensure_ascii=False, indent=2))

    def sample():
        raw = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,rss=,command='],
                             capture_output=True, text=True, timeout=3)
        processes = []
        for line in raw.stdout.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) != 4:
                continue
            pid, parent, rss, command = parts
            if ((app and int(pid) == app.pid) or (worker and int(pid) == worker.pid)
                    or Path(command.split()[0]).name.lower() in ('ollama', 'llama-server')):
                processes.append({'pid': int(pid), 'ppid': int(parent),
                                  'rss_bytes': int(rss)*1024, 'command': command})
        system = {}
        for key, cmd in [('vm', ['/usr/bin/vm_stat']),
                         ('swap', ['/usr/sbin/sysctl', 'vm.swapusage']),
                         ('pressure', ['/usr/bin/memory_pressure', '-Q'])]:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            system[key] = r.stdout.strip() if r.returncode == 0 else r.stderr.strip()
        value = {'time': time.time(), 'stage': stage, 'processes': processes,
                 'ollama': api('ps'), 'system': system, 'ps_exit': raw.returncode}
        measurements.append(value)
        with (out/'samples.jsonl').open('a') as f:
            f.write(json.dumps(value, ensure_ascii=False)+'\n')

    def monitor():
        while not finished.is_set():
            sample()
            finished.wait(5)

    def run(name, command, cancel=False):
        nonlocal worker, stage
        stage = name
        started = time.monotonic()
        stopped_at = None
        with (out/(name+'.log')).open('wb') as log:
            worker = subprocess.Popen(prefix + command, env=env, cwd=out, stdout=log, stderr=log)
            while worker.poll() is None:
                if cancel and stopped_at is None:
                    snapshot = out/name/'snapshot.json'
                    try:
                        if json.loads(snapshot.read_text()).get('received_audio_seconds', 0) >= 5:
                            stopped_at = time.monotonic()
                            worker.send_signal(signal.SIGTERM)
                    except (OSError, ValueError):
                        pass
                if time.monotonic()-started > 480:
                    worker.send_signal(signal.SIGTERM)
                    worker.wait(timeout=190)
                    raise RuntimeError('Finite trial timed out; evidence retained')
                time.sleep(.2)
            result = {'name': name, 'exit': worker.returncode,
                      'elapsed_seconds': time.monotonic()-started,
                      'safe_stop_seconds': time.monotonic()-stopped_at if stopped_at else None}
        session = out/name
        if (session/'session.sqlite').exists():
            db = sqlite3.connect(session/'session.sqlite')
            result['integrity'] = db.execute('pragma integrity_check').fetchone()[0]
            result['snapshot'] = json.loads((session/'snapshot.json').read_text())
            db.close()
        operations.append(result)
        (out/'operations.json').write_text(json.dumps(operations, ensure_ascii=False, indent=2))
        print(json.dumps({k:v for k,v in result.items() if k != 'snapshot'}), flush=True)
        worker = None
        if result['exit'] != 0:
            raise RuntimeError(f"{name} failed: see retained operation log")
        stage = name+'_after_exit'
        time.sleep(10)
        return result

    def speech_command(command, name, extra):
        # Preserve the venv executable spelling for the old-environment baseline;
        # resolving its symlink would launch the base interpreter without its libs.
        bootstrap = ('import sys; sys.path.insert(0, '+repr(str(args.runtime.resolve()))+');\n') if env.get('LOCAL_TRANSLATOR_RUNTIME_ROOT') else ''
        isolated = ['-I', '-B'] if bootstrap else []
        return [str(args.python.absolute())] + isolated + ['-c', bootstrap+DRIVER, command, '--session', str(out/name)] + extra

    thread = threading.Thread(target=monitor, daemon=True)
    try:
        with (out/'app.log').open('wb') as log:
            app_arguments = ['--environment-root', str(args.environment_root.resolve())] if args.environment_root else []
            if args.resource_root: app_arguments += ['--resource-root', str(args.resource_root.resolve())]
            app = subprocess.Popen(prefix+[str(args.app.resolve()/'Contents/MacOS/LocalTranslatorApp'),
                '--recovery-root', str(out/'text-tasks'), '--recordings-root', str(out/'recordings')]+app_arguments,
                cwd=out, env=env, stdout=log, stderr=log)
        thread.start()
        stage = 'pretrial_shared_natural_expiry'
        deadline = time.monotonic()+360
        if args.speech_only:
            if not any(m.get('digest') == '202e0ccbae412a107d24c32bc08d7674c579c459a5942e7ed42e5fff631630d9' for m in initial.get('models', [])):
                raise RuntimeError('Focused follow-up requires the same resident 7B model')
        while not args.speech_only and api('ps').get('models'):
            if time.monotonic() > deadline:
                raise RuntimeError('Shared model still active; cold trial postponed without unloading it')
            time.sleep(5)
        (out/('resident-start-state.json' if args.speech_only else 'cold-start-state.json')).write_text(json.dumps(api('ps')))
        stage = 'app_idle_no_inference'
        time.sleep(15)
        text = out/'text.txt'
        text.write_text('Keep the original recording until the new copy has been checked. '
                        'The meeting starts at nine and ends at ten.\n')
        cli = str(args.app.resolve()/'Contents/MacOS/translator-m0')
        if not args.speech_only:
            run('text_cold', [cli, 'translate', '--input', str(text), '--model', 'hy-mt2:7b-q8',
                             '--direction', 'en-zh', '--output', str(out/'text-cold.json')])
            run('text_warm', [cli, 'translate', '--input', str(text), '--model', 'hy-mt2:7b-q8',
                             '--direction', 'en-zh', '--output', str(out/'text-warm.json')])
        run('speech', speech_command('run', 'speech', ['--config', str(args.config.resolve()),
                                                     '--input', str(args.input.resolve())]))
        run('refinement', speech_command('refine', 'refinement', ['--config', str(args.config.resolve()),
                                            '--from-session', str(out/'speech')]))
        run('cancel', speech_command('run', 'cancel', ['--config', str(args.config.resolve()),
                                '--input', str(args.input.resolve()), '--paced']), cancel=True)
        stage = 'idle_existing_keep_alive_5m' if args.idle_seconds >= 330 else f'idle_followup_{args.idle_seconds}s'
        print(f'Observing {args.idle_seconds}s idle window; shared service untouched.', flush=True)
        finished.wait(args.idle_seconds)
        if not args.speech_only:
            run('text_reload', [cli, 'translate', '--input', str(text), '--model', 'hy-mt2:7b-q8',
                               '--direction', 'en-zh', '--output', str(out/'text-reload.json')])
        stage = 'app_exit'
        app.terminate()
        app.wait(timeout=15)
        app = None
        time.sleep(10)
    finally:
        finished.set()
        if thread.is_alive():
            thread.join(timeout=15)
        for child in (worker, app):
            if child and child.poll() is None:
                child.terminate()
                child.wait(timeout=190)
    (out/'summary.json').write_text(json.dumps({
        'operations': [{k:v for k,v in op.items() if k != 'snapshot'} for op in operations],
        'samples': len(measurements), 'finished': True,
        'peaks': {str(pid): max(p['rss_bytes'] for s in measurements for p in s['processes']
                              if p['pid'] == pid)
                  for pid in {p['pid'] for s in measurements for p in s['processes']}}
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
