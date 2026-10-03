#!/usr/bin/env python3
"""Assemble a new complete portable copy; original interpreter/venv/seal untouched."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for data in iter(lambda:f.read(1024*1024), b''):
            h.update(data)
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--assets-output', type=Path, required=True)
    args = p.parse_args()
    project = Path(__file__).resolve().parent.parent
    env = project/'artifacts/whisperlivekit-gpu-review-20260915/venv'
    base = (env/'bin/python').resolve().parent.parent
    source = project/'artifacts/whisperlivekit-speech-repair-20261001-final/source'
    seal = source.parent/'patched-source.json'
    fingerprints = json.loads(seal.read_text())
    for name, expected in fingerprints['patched_files'].items():
        assert sha(source/name) == expected, f'Seal mismatch: {name}'
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    assets = args.assets_output.resolve(); assets.mkdir(parents=True, exist_ok=False)
    shutil.copytree(base, out/'Python', symlinks=True)
    site = out/'Python/lib/python3.12/site-packages'
    shutil.copytree(env/'lib/python3.12/site-packages', site, symlinks=True, dirs_exist_ok=True)
    code = out/'Code'; code.mkdir()
    for name in fingerprints['patched_files']:
        target = code/'source'/name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source/name, target)
    shutil.copyfile(source/'LICENSE', code/'source/LICENSE')
    shutil.copyfile(seal, code/'pinned-source.json')
    shutil.copyfile(project/'experiments/whisperlivekit/mps-model-manifest.json', code/'model-manifest.json')
    shutil.copyfile(project/'scripts/runtime-selfcheck.py', code/'selfcheck.py')
    asset_files = {}
    for original, relative in [(source/'whisperlivekit/whisper/assets', 'wlk/whisper/assets'),
        (source/'whisperlivekit/silero_vad_models', 'wlk/silero_vad_models'),
        (env/'lib/python3.12/site-packages/mlx_whisper/assets', 'mlx/assets')]:
        for path in original.iterdir():
            if path.is_file() and path.suffix in ['.onnx','.jit','.npz','.tiktoken']:
                name = str(Path(relative)/path.name)
                target = assets/name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                asset_files[name] = {'size': path.stat().st_size, 'sha256': sha(path)}
    for path in list((site/'mlx_whisper/assets').glob('*')):
        if path.is_file() and path.suffix in ['.onnx','.jit','.npz','.tiktoken']:
            path.unlink() # ONLY the new runtime copy, verified external twins above.
    excluded_model_data = []
    for path in list((site/'onnxruntime/datasets').glob('*.onnx')) + list((site/'faster_whisper/assets').glob('*.onnx')):
        entry = {'path':str(path.relative_to(out)), 'bytes':path.stat().st_size,
                 'sha256':sha(path), 'reason':'model data excluded from software; unused by fixed App route'}
        if 'faster_whisper' in str(path):
            target = assets/'optional-faster-whisper'/path.name
            target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(path,target)
            entry['external_optional_copy'] = str(target.relative_to(assets))
        excluded_model_data.append(entry)
        path.unlink()
    asset_manifest = {'schema': 1, 'id': 'speech-data-v1', 'files': asset_files}
    (code/'assets-manifest.json').write_text(json.dumps(asset_manifest, indent=2)+'\n')
    (assets/'assets-manifest.json').write_text(json.dumps(asset_manifest, indent=2)+'\n')
    dependencies = {}
    for line in (project/'experiments/whisperlivekit/requirements-mps-trial.txt').read_text().splitlines():
        if '==' in line:
            name, version = line.split('=='); dependencies[name] = version
    manifest = {'schema': 1, 'id': 'speech-runtime', 'version': '0.2.10-r1-full',
        'platform': 'macos-arm64', 'minimum_macos': '26.0', 'python': '3.12.14',
        'interpreter_build': (base/'BUILD').read_text(), 'dependencies': dependencies,
        'source_manifest_sha256': sha(seal), 'external_assets': asset_manifest,
        'models_included': False, 'complete_baseline': True, 'excluded_model_data':excluded_model_data}
    (out/'component.json').write_text(json.dumps(manifest, indent=2)+'\n')
    # No environment/global mutation. Make every archive link relative and local.
    for path in out.rglob('*'):
        if path.is_symlink():
            target = path.resolve()
            assert target.is_relative_to(out), f'Escaping runtime link: {path}'
    data = [p for p in out.rglob('*') if p.is_file() and not p.is_symlink()]
    (out.parent/(out.name+'-size.json')).write_text(json.dumps({'files':len(data),
        'bytes':sum(p.stat().st_size for p in data), 'assets_bytes':sum(v['size'] for v in asset_files.values()),
        'source_files':len(fingerprints['patched_files'])}, indent=2)+'\n')
    print(json.dumps({'runtime':str(out), 'external_assets':str(assets), 'files':len(data),
                      'bytes':sum(p.stat().st_size for p in data)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
