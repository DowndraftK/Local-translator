#!/usr/bin/env python3
"""Bounded import/platform check for a fixed managed runtime; no model load."""
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys

root = Path(__file__).resolve().parent.parent
manifest = json.loads((root/'component.json').read_text())
assert sys.version_info[:3] == (3, 12, 14), 'Fixed Python version mismatch'
assert platform.machine() == 'arm64', 'Requires arm64'
assert sys.flags.isolated and sys.flags.no_user_site, 'Requires isolated interpreter'
assert Path(sys.prefix).resolve() == (root/'Python').resolve(), 'Interpreter is not independent'
sys.path.insert(0, str(root/'Code/source'))
for name, version in manifest['dependencies'].items():
    assert importlib.metadata.version(name) == version, f'Fixed dependency mismatch: {name}'
fingerprints = json.loads((root/'Code/pinned-source.json').read_text())
for name, expected in fingerprints['patched_files'].items():
    path = root/'Code/source'/name
    if name in manifest.get('omitted_test_tools', {}):
        assert manifest['omitted_test_tools'][name] == expected and not path.exists(), f'Test-tool difference mismatch: {name}'
        continue
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, f'Sealed source mismatch: {name}'
origins = {}
for name in ['torch','mlx.core','onnxruntime','av','soundfile','tiktoken','mlx_whisper','whisperlivekit.core']:
    module = importlib.import_module(name)
    origins[name] = str(Path(module.__file__).resolve())
    assert Path(origins[name]).is_relative_to(root), f'External code import: {name}'
import onnxruntime
onnxruntime.disable_telemetry_events()
import torch
import mlx.core as mx
assert torch.backends.mps.is_available(), 'MPS unavailable; no CPU fallback'
assert mx.default_device() == mx.gpu, 'MLX GPU unavailable'
print(json.dumps({'ok': True, 'python': sys.version, 'executable': sys.executable,
    'prefix': sys.prefix, 'platform': platform.machine(), 'isolated': True,
    'versions': manifest['dependencies'], 'origins': origins, 'mps': True, 'mlx_gpu': True,
    'source_files': len(fingerprints['patched_files'])}, ensure_ascii=False))
