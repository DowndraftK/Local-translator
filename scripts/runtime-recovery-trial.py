#!/usr/bin/env python3
"""Finite copy-only recovery proof with a real selected bundle/runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

DRIVER = '''
import streaming_translator.__main__ as worker
original = worker.translation_loop
async def serial_translation(store, done, stop):
    await done.wait()
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


def saved(directory):
    db = sqlite3.connect(directory/'session.sqlite')
    try:
        return {'recipe': json.loads(db.execute("select value from metadata where key='translation_configuration'").fetchone()[0]),
            'success_rows': db.execute("select * from translations where state='completed' order by segment_id").fetchall(),
            'schema': db.execute('pragma user_version').fetchone()[0],
            'integrity': db.execute('pragma integrity_check').fetchone()[0],
            'history': db.execute('select count(*) from recovery_history').fetchone()[0],
            'config_hash': digest(directory/'runtime.json'), 'audio_hash': digest(directory/'audio.wav')}
    finally:
        db.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline', type=Path, required=True)
    p.add_argument('--runtime', type=Path, required=True)
    p.add_argument('--python', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--isolation-profile', type=Path)
    args = p.parse_args()
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    env = {k:v for k,v in os.environ.items() if k not in ['PYTHONHOME','PYTHONPATH','VIRTUAL_ENV','CONDA_PREFIX', 'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH', 'PYTHONUSERBASE']}
    env.update(PYTHONPATH=str(args.runtime.resolve()), PYTHONDONTWRITEBYTECODE='1',
               PYTHONNOUSERSITE='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
               PYTORCH_ENABLE_MPS_FALLBACK='0')
    results = {}
    prefix = ['/usr/bin/sandbox-exec', '-f', str(args.isolation_profile.resolve())] if args.isolation_profile else []
    bootstrap = ('import sys; sys.path.insert(0, '+repr(str(args.runtime.resolve()))+');\n') if env.get('LOCAL_TRANSLATOR_RUNTIME_ROOT') else ''
    python = [str(args.python.absolute())] + (['-I', '-B'] if bootstrap else [])
    for origin, name, command in [('speech','old-success','retry'), ('cancel','old-cancel','resume')]:
        target = out/name; shutil.copytree(args.baseline/origin, target)
        before = saved(target)
        with (out/(name+'.log')).open('wb') as log:
            process = subprocess.run(prefix+python+['-c', bootstrap+DRIVER, command,
                '--session', str(target)], env=env, cwd=out, stdout=log, stderr=log, timeout=240)
        after = saved(target)
        assert process.returncode == 0
        assert after['schema'] == before['schema'] == 3 and after['integrity'] == 'ok'
        assert after['audio_hash'] == before['audio_hash'] and after['config_hash'] == before['config_hash']
        if command == 'retry':
            assert before['success_rows'] and after['success_rows'] == before['success_rows']
            assert after['recipe'] == before['recipe']
        else:
            expected = dict(before['recipe'])
            if expected.get('model_digest') is None:
                expected['model_digest'] = after['recipe']['model_digest']
            assert after['recipe'] == expected
            assert len(after['success_rows']) == 3
            assert after['history'] > before['history']
        for kind in ('txt','srt','vtt'):
            with (out/(name+'-export.log')).open('ab') as log:
                entry = bootstrap+'import runpy; runpy.run_module("streaming_translator", run_name="__main__")' if bootstrap else None
                module = ['-c', entry] if entry else ['-m', 'streaming_translator']
                r = subprocess.run(prefix+python+module+['export',
                    '--session',str(target),'--format',kind,'--output',str(out/(name+'.'+kind))],
                    env=env,cwd=out,stdout=log,stderr=log,timeout=30)
            assert r.returncode == 0 and (out/(name+'.'+kind)).stat().st_size > 0
        results[name] = {'exit': process.returncode, 'schema': after['schema'], 'integrity': after['integrity'],
            'audio_unchanged': True, 'runtime_json_unchanged': True, 'successful_rows_before': len(before['success_rows']),
            'successful_rows_after': len(after['success_rows']), 'existing_success_rows_unchanged': command == 'retry',
            'recipe_preserved_except_first_unknown_digest_binding': True,
            'recovery_history_added': after['history']-before['history'], 'exports': ['txt','srt','vtt']}
        print(json.dumps({name:results[name]},ensure_ascii=False),flush=True)
    (out/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')


if __name__ == '__main__':
    main()
